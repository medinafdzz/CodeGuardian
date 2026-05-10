from codeguardian.improvements import align_issue_to_current_file, changed_files, improvements_enabled
from codeguardian.models import Issue


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
