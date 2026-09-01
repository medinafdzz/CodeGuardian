import json

from codeguardian.models import Decision, Issue
from codeguardian.results_export import build_results_export, write_results_export


def make_issue(**overrides):
    data = {
        "sonar_key": "S1",
        "file": "app.py",
        "target_type": "function",
        "target_name": "run",
        "line": 2,
        "original_start_line": 2,
        "original_end_line": 2,
        "problem": "Unsafe value",
        "severity": "CRITICAL",
        "solution": "Use safe value",
        "original_code": "    return unsafe",
        "proposed_code": "    return safe",
    }
    data.update(overrides)
    return Issue(**data)


def test_build_results_export_creates_stable_valid_json(tmp_path):
    decision = Decision(issues=[make_issue()])

    data = build_results_export(decision, "project", "repo", "workspace", "7", True)
    output = tmp_path / "nested" / "codeguardian-results.json"
    write_results_export(output, data)

    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == "1.0"
    assert loaded["summary"]["total_suggestions"] == 1
    assert loaded["summary"]["blocking_findings"] is True
    assert loaded["suggestions"][0]["id"]
    assert loaded["suggestions"][0]["content_hash"]
    assert loaded["suggestions"][0]["status"] == "open"


def test_build_results_export_uses_git_commit_env(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "abc123")
    decision = Decision(issues=[make_issue()])

    data = build_results_export(decision, "project", "repo", "workspace", "7", True)

    assert data["head_commit"] == "abc123"


def test_build_results_export_prefers_codeguardian_head_commit(monkeypatch):
    monkeypatch.setenv("CODEGUARDIAN_HEAD_COMMIT", "pr-head")
    monkeypatch.setenv("GIT_COMMIT", "jenkins-merge")
    decision = Decision(issues=[make_issue()])

    data = build_results_export(decision, "project", "repo", "workspace", "7", True)

    assert data["head_commit"] == "pr-head"
