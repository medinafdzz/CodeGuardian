import argparse

from codeguardian.ai import analyze_code_with_gemini
from codeguardian.bitbucket import report_to_bitbucket
from codeguardian.input_contract import load_webhook_data
from codeguardian.logging_utils import logger
from codeguardian.metrics import push_execution_metrics
from codeguardian.models import AgentExecutionError, AnalysisMetrics, Decision, ExecutionMetrics
from codeguardian.sonarqube import fetch_sonar_issues
from codeguardian.validation import filter_valid_issues, normalize_issues, validate_maven_compile


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Path to the JSON file to analyze")
    args = parser.parse_args()

    project_key, pr_id, repo_slug, workspace = load_webhook_data(args.file)
    if not project_key or not pr_id or not repo_slug:
        logger.error("Project key, pull request ID or repo slug not found in the JSON file.")
        raise AgentExecutionError("Webhook payload is missing required fields")

    issues = await fetch_sonar_issues(project_key)

    if not issues:
        logger.info("No relevant issues found by SonarQube. Reporting clean analysis state to the pull request.")
        decision = Decision(issues=[])
        comments = await report_to_bitbucket(pr_id, repo_slug, workspace, decision)
        push_execution_metrics(
            project_key,
            AnalysisMetrics(),
            ExecutionMetrics(),
            comments,
        )
        return

    logger.info(f"Relevant issues found by SonarQube: {len(issues)}. Proceeding with AI analysis.")

    has_blocking_findings = any(str(issue.get("severity", "")).upper() in {"BLOCKER", "CRITICAL"} for issue in issues)

    decision = analyze_code_with_gemini(project_key, issues)

    decision.issues, invalid_count = normalize_issues(decision.issues)
    decision.issues, patch_invalid_count = filter_valid_issues(decision.issues)

    if invalid_count:
        logger.info("Dropped %s invalid issues", invalid_count)

    if patch_invalid_count:
        logger.info("Dropped %s issues after patch validation", patch_invalid_count)

    validate_maven_compile()

    logger.info(
        "Execution summary: sonar_findings=%s generated_issues=%s dropped_invalid=%s dropped_patch_validation=%s final_issues=%s blocking_findings=%s",
        len(issues),
        len(decision.issues) + invalid_count + patch_invalid_count,
        invalid_count,
        patch_invalid_count,
        len(decision.issues),
        has_blocking_findings,
    )

    comments = await report_to_bitbucket(pr_id, repo_slug, workspace, decision)
    push_execution_metrics(
        project_key,
        decision.metrics,
        ExecutionMetrics(
            sonar_findings=len(issues),
            generated_issues=len(decision.issues) + invalid_count + patch_invalid_count,
            invalid_issues=invalid_count,
            patch_invalid_issues=patch_invalid_count,
            final_issues=len(decision.issues),
            blocking_findings=has_blocking_findings,
        ),
        comments,
    )
