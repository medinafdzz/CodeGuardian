from prometheus_client import generate_latest

from codeguardian.metrics import build_metrics_registry, metric_grouping_key
from codeguardian.models import AnalysisMetrics, CommentSyncResult, ExecutionMetrics


def test_build_metrics_registry_exports_execution_analysis_and_comment_values():
    registry = build_metrics_registry(
        analysis=AnalysisMetrics(
            latency_seconds=2.5,
            prompt_tokens=100,
            response_tokens=40,
            total_tokens=140,
            cached_tokens=60,
            batch_cache_hits=3,
            batch_cache_misses=1,
            improvement_candidates=4,
        ),
        execution=ExecutionMetrics(
            sonar_findings=10,
            generated_issues=5,
            invalid_issues=1,
            patch_invalid_issues=2,
            final_issues=2,
            blocking_findings=True,
        ),
        comments=CommentSyncResult(
            desired=2,
            created=1,
            reused=1,
            deleted=3,
        ),
    )

    output = generate_latest(registry).decode("utf-8")

    assert "codeguardian_analysis_latency_seconds 2.5" in output
    assert "codeguardian_analysis_prompt_tokens 100.0" in output
    assert "codeguardian_analysis_cached_tokens 60.0" in output
    assert "codeguardian_batch_cache_hits_total 3.0" in output
    assert "codeguardian_improvement_candidates_total 4.0" in output
    assert "codeguardian_sonar_findings_total 10.0" in output
    assert "codeguardian_final_issues_total 2.0" in output
    assert "codeguardian_blocking_findings 1.0" in output
    assert "codeguardian_comments_created_total 1.0" in output
    assert "codeguardian_comments_deleted_total 3.0" in output


def test_metric_grouping_key_uses_stable_repository_labels(monkeypatch):
    monkeypatch.setenv("BUILD_NUMBER", "58")

    grouping_key = metric_grouping_key("demo-java", current_timestamp=1780000000.2)

    assert grouping_key == {
        "event_type": "pull_request",
        "repository": "demo-java",
    }
