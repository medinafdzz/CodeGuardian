from codeguardian.improvements import align_issue_to_current_file, changed_files, improvements_enabled
from codeguardian.models import ImprovementCandidate, Issue


def test_improvements_enabled_accepts_true_values(monkeypatch):
    monkeypatch.setenv("CODEGUARDIAN_ENABLE_IMPROVEMENTS", "true")

    assert improvements_enabled() is True


def test_improvements_enabled_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CODEGUARDIAN_ENABLE_IMPROVEMENTS", raising=False)

    assert improvements_enabled() is False


def test_changed_files_filters_generated_and_missing_paths(monkeypatch, tmp_path):
    (tmp_path / "service.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def fake_run_git(args):
        assert args == ["diff", "--name-only", "--diff-filter=AM", "origin/main...HEAD"]
        return "\n".join([
            "service.py",
            "node_modules/generated.js",
            "missing.py",
        ])

    monkeypatch.setattr("codeguardian.improvements.run_git", fake_run_git)

    assert changed_files("origin/main...HEAD", max_files=5) == ["service.py"]


def test_changed_files_uses_configurable_exclusions(monkeypatch, tmp_path):
    for path in [
        "service.py",
        "generated/client.py",
        "essFramework/release/runtime.py",
        "models/service_pb2.py",
    ]:
        file_path = tmp_path / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "CODEGUARDIAN_IMPROVEMENT_EXCLUDE",
        "generated/,essFramework/release/,*_pb2.py",
    )

    def fake_run_git(args):
        assert args == ["diff", "--name-only", "--diff-filter=AM", "origin/main...HEAD"]
        return "\n".join([
            "service.py",
            "generated/client.py",
            "essFramework/release/runtime.py",
            "models/service_pb2.py",
        ])

    monkeypatch.setattr("codeguardian.improvements.run_git", fake_run_git)

    assert changed_files("origin/main...HEAD", max_files=5) == ["service.py"]


def test_improvement_candidate_keeps_detection_evidence():
    candidate = ImprovementCandidate(
        file="service.py",
        line=12,
        language="python",
        category="complexity",
        reason="Function has too many nested branches.",
        evidence="nesting_depth=4",
        original_code="def run():\n    if enabled:\n        return True",
        confidence=0.8,
    )

    assert candidate.file == "service.py"
    assert candidate.line == 12
    assert candidate.language == "python"
    assert candidate.category == "complexity"
    assert candidate.evidence == "nesting_depth=4"
    assert candidate.confidence == 0.8


def test_align_issue_to_current_file_updates_imprecise_line(monkeypatch, tmp_path):
    source = tmp_path / "service.py"
    source.write_text("def first():\n    return 1\n\n\ndef second():\n    return 2\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    issue = Issue(
        sonar_key="IMPROVEMENT:1",
        file="service.py",
        target_type="function",
        target_name="second",
        line=1,
        original_start_line=1,
        original_end_line=2,
        problem="The line is imprecise.",
        severity="IMPROVEMENT",
        solution="Use the exact block location.",
        original_code="def second():\n    return 2",
        proposed_code="def second():\n    value = 2\n    return value",
    )

    aligned = align_issue_to_current_file(issue)

    assert aligned.line == 5
    assert aligned.original_start_line == 5
    assert aligned.original_end_line == 6
