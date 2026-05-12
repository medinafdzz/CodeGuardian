from codeguardian.models import AnalysisMetrics
from codeguardian.runtime import combine_analysis_metrics


def test_combine_analysis_metrics_preserves_improvement_candidates():
    metrics = combine_analysis_metrics(
        AnalysisMetrics(prompt_tokens=10, improvement_candidates=2),
        AnalysisMetrics(response_tokens=5, improvement_candidates=3),
    )

    assert metrics.prompt_tokens == 10
    assert metrics.response_tokens == 5
    assert metrics.improvement_candidates == 5
