import json

from agent import load_webhook_data


def test_load_webhook_data_returns_required_pull_request_context(tmp_path):
    payload = {
        "project_key": "codeguardian-demo",
        "pr_id": 42,
        "repo_slug": "some-repository",
        "workspace": "engineering-workspace",
    }
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps(payload), encoding="utf-8")

    assert load_webhook_data(str(data_file)) == (
        "codeguardian-demo",
        "42",
        "some-repository",
        "engineering-workspace",
    )


def test_load_webhook_data_uses_default_workspace(tmp_path):
    payload = {
        "project_key": "codeguardian-demo",
        "pr_id": "15",
        "repo_slug": "target-repository",
    }
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps(payload), encoding="utf-8")

    assert load_webhook_data(str(data_file)) == (
        "codeguardian-demo",
        "15",
        "target-repository",
        "medinafdzz",
    )


def test_load_webhook_data_returns_empty_context_when_required_field_is_missing(tmp_path):
    payload = {
        "project_key": "codeguardian-demo",
        "repo_slug": "target-repository",
    }
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps(payload), encoding="utf-8")

    assert load_webhook_data(str(data_file)) == ("", "", "", "")
