from codeguardian.models import AnalysisMetrics
from codeguardian.runtime import combine_analysis_metrics


def test_combine_analysis_metrics_preserves_performance_review_counts():
    metrics = combine_analysis_metrics(
        AnalysisMetrics(prompt_tokens=10, performance_candidates=1),
        AnalysisMetrics(response_tokens=5, performance_suggestions=1),
    )

    assert metrics.prompt_tokens == 10
    assert metrics.response_tokens == 5
    assert metrics.performance_candidates == 1
    assert metrics.performance_suggestions == 1
