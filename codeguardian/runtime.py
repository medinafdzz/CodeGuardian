import argparse

from codeguardian.ai import analyze_code_with_gemini
from codeguardian.bitbucket import report_to_bitbucket
from codeguardian.input_contract import load_webhook_data
from codeguardian.logging_utils import logger
from codeguardian.metrics import push_execution_metrics
from codeguardian.models import AgentExecutionError, AnalysisMetrics, Decision, ExecutionMetrics
from codeguardian.performance import analyze_performance, performance_enabled
from codeguardian.sonarqube import fetch_sonar_issues
from codeguardian.validation import filter_valid_issues, normalize_issues, validate_maven_compile


def combine_analysis_metrics(*metrics: AnalysisMetrics) -> AnalysisMetrics:
    return AnalysisMetrics(
        latency_seconds=sum(metric.latency_seconds for metric in metrics),
        prompt_tokens=sum(metric.prompt_tokens for metric in metrics),
        response_tokens=sum(metric.response_tokens for metric in metrics),
        total_tokens=sum(metric.total_tokens for metric in metrics),
        cached_tokens=sum(metric.cached_tokens for metric in metrics),
        batch_cache_hits=sum(metric.batch_cache_hits for metric in metrics),
        batch_cache_misses=sum(metric.batch_cache_misses for metric in metrics),
        performance_candidates=sum(metric.performance_candidates for metric in metrics),
        performance_suggestions=sum(metric.performance_suggestions for metric in metrics),
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Path to the JSON file to analyze")
    args = parser.parse_args()

    project_key, pr_id, repo_slug, workspace = load_webhook_data(args.file)
    if not project_key or not pr_id or not repo_slug:
        logger.error("Project key, pull request ID or repo slug not found in the JSON file.")
        raise AgentExecutionError("Webhook payload is missing required fields")

    sonar_issues = await fetch_sonar_issues(project_key)
    generated_static_count = 0
    invalid_count = 0
    patch_invalid_count = 0
    has_blocking_findings = any(
        str(issue.get("severity", "")).upper() in {"BLOCKER", "CRITICAL"} for issue in sonar_issues
    )
    decision = Decision(issues=[])
    analysis_metrics = AnalysisMetrics()

    if sonar_issues:
        logger.info(f"Relevant issues found by SonarQube: {len(sonar_issues)}. Proceeding with AI analysis.")

        static_decision = analyze_code_with_gemini(project_key, sonar_issues)

        static_decision.issues, invalid_count = normalize_issues(static_decision.issues)
        static_decision.issues, patch_invalid_count = filter_valid_issues(static_decision.issues)
        generated_static_count = len(static_decision.issues) + invalid_count + patch_invalid_count

        if invalid_count:
            logger.info("Dropped %s invalid issues", invalid_count)

        if patch_invalid_count:
            logger.info("Dropped %s issues after patch validation", patch_invalid_count)

        decision.issues.extend(static_decision.issues)
        analysis_metrics = combine_analysis_metrics(analysis_metrics, static_decision.metrics)
    else:
        logger.info("No relevant issues found by SonarQube.")

    performance_review_enabled = performance_enabled()
    logger.info("Performance review enabled: %s", performance_review_enabled)

    if performance_review_enabled:
        performance_decision = analyze_performance(project_key)
        performance_decision.issues, performance_invalid_count = normalize_issues(performance_decision.issues)
        performance_decision.issues, performance_patch_invalid_count = filter_valid_issues(performance_decision.issues)

        if performance_invalid_count:
            logger.info("Dropped %s invalid performance suggestions", performance_invalid_count)

        if performance_patch_invalid_count:
            logger.info(
                "Dropped %s performance suggestions after patch validation",
                performance_patch_invalid_count,
            )

        logger.info("Final performance comments: %s", len(performance_decision.issues))
        decision.issues.extend(performance_decision.issues)
        analysis_metrics = combine_analysis_metrics(analysis_metrics, performance_decision.metrics)

    validate_maven_compile()

    logger.info(
        "Execution summary: sonar_findings=%s generated_issues=%s dropped_invalid=%s dropped_patch_validation=%s final_issues=%s blocking_findings=%s performance_suggestions=%s",
        len(sonar_issues),
        generated_static_count,
        invalid_count,
        patch_invalid_count,
        len(decision.issues),
        has_blocking_findings,
        len([issue for issue in decision.issues if issue.source == "performance"]),
    )

    comments = await report_to_bitbucket(pr_id, repo_slug, workspace, decision)
    push_execution_metrics(
        project_key,
        analysis_metrics,
        ExecutionMetrics(
            sonar_findings=len(sonar_issues),
            generated_issues=generated_static_count,
            invalid_issues=invalid_count,
            patch_invalid_issues=patch_invalid_count,
            final_issues=len(decision.issues),
            blocking_findings=has_blocking_findings,
        ),
        comments,
    )
