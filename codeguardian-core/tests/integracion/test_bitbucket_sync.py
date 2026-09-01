import pytest

from codeguardian.bitbucket import delete_inline_comment_by_rest, synchronize_inline_comments
from codeguardian.comments import comment_content
from codeguardian.models import Issue


def make_issue(**overrides):
    data = {
        "sonar_key": "S1",
        "file": "src/service.py",
        "target_type": "function",
        "target_name": "calculate",
        "line": 2,
        "original_start_line": 2,
        "original_end_line": 2,
        "problem": "Problem description",
        "severity": "MAJOR",
        "solution": "Solution description",
        "original_code": "value = 1",
        "proposed_code": "value = 2",
        "required_imports": [],
    }
    data.update(overrides)
    return Issue(**data)


def existing_comment(issue: Issue, comment_id: int = 10) -> dict:
    raw = comment_content([issue])
    return {
        "id": comment_id,
        "content": {"raw": raw},
        "inline": {"path": issue.file, "to": issue.line},
    }


@pytest.mark.asyncio
async def test_delete_inline_comment_uses_bitbucket_delete_endpoint(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("BITBUCKET_EMAIL", "bot@example.com")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "token")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    deleted = await delete_inline_comment_by_rest("7", "repo", "10", "workspace")

    assert deleted is True
    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/repositories/workspace/repo/pullrequests/7/comments/10")


@pytest.mark.asyncio
async def test_obsolete_codeguardian_comments_are_resolved_by_default(monkeypatch, tmp_path):
    old_file = tmp_path / "old.py"
    new_file = tmp_path / "new.py"
    old_file.write_text("old = True\n", encoding="utf-8")
    new_file.write_text("new = True\n", encoding="utf-8")
    old_issue = make_issue(sonar_key="OLD", file=str(old_file), line=1)
    new_issue = make_issue(sonar_key="NEW", file=str(new_file), line=1)
    resolved: list[int] = []
    deleted: list[int] = []

    async def fake_get_pull_request_comments(*_args):
        return [existing_comment(old_issue, 10)]

    async def fake_resolve_comment_ids(_pr_id, _repo_slug, _workspace, comment_ids):
        resolved.extend(sorted(comment_ids))
        return set(comment_ids), set()

    async def fake_delete_comment_ids(_pr_id, _repo_slug, _workspace, comment_ids):
        deleted.extend(sorted(comment_ids))
        return set(comment_ids), set()

    async def fake_post_issue_group_comment(*_args, **_kwargs):
        return True

    monkeypatch.setattr("codeguardian.bitbucket.get_pull_request_comments", fake_get_pull_request_comments)
    monkeypatch.setattr("codeguardian.bitbucket.resolve_comment_ids", fake_resolve_comment_ids)
    monkeypatch.setattr("codeguardian.bitbucket.delete_comment_ids", fake_delete_comment_ids)
    monkeypatch.setattr("codeguardian.bitbucket.post_issue_group_comment", fake_post_issue_group_comment)

    result = await synchronize_inline_comments(None, "7", "repo", "workspace", [new_issue])

    assert resolved == [10]
    assert deleted == []
    assert result.resolved == 1
    assert result.deleted == 0
    assert result.created == 1


@pytest.mark.asyncio
async def test_obsolete_codeguardian_comments_can_still_be_deleted(monkeypatch, tmp_path):
    old_file = tmp_path / "old.py"
    new_file = tmp_path / "new.py"
    old_file.write_text("old = True\n", encoding="utf-8")
    new_file.write_text("new = True\n", encoding="utf-8")
    old_issue = make_issue(sonar_key="OLD", file=str(old_file), line=1)
    new_issue = make_issue(sonar_key="NEW", file=str(new_file), line=1)
    resolved: list[int] = []
    deleted: list[int] = []

    async def fake_get_pull_request_comments(*_args):
        return [existing_comment(old_issue, 10)]

    async def fake_resolve_comment_ids(_pr_id, _repo_slug, _workspace, comment_ids):
        resolved.extend(sorted(comment_ids))
        return set(comment_ids), set()

    async def fake_delete_comment_ids(_pr_id, _repo_slug, _workspace, comment_ids):
        deleted.extend(sorted(comment_ids))
        return set(comment_ids), set()

    async def fake_post_issue_group_comment(*_args, **_kwargs):
        return True

    monkeypatch.setenv("CODEGUARDIAN_COMMENT_SYNC_MODE", "delete")
    monkeypatch.setattr("codeguardian.bitbucket.get_pull_request_comments", fake_get_pull_request_comments)
    monkeypatch.setattr("codeguardian.bitbucket.resolve_comment_ids", fake_resolve_comment_ids)
    monkeypatch.setattr("codeguardian.bitbucket.delete_comment_ids", fake_delete_comment_ids)
    monkeypatch.setattr("codeguardian.bitbucket.post_issue_group_comment", fake_post_issue_group_comment)

    result = await synchronize_inline_comments(None, "7", "repo", "workspace", [new_issue])

    assert resolved == []
    assert deleted == [10]
    assert result.resolved == 0
    assert result.deleted == 1


@pytest.mark.asyncio
async def test_obsolete_codeguardian_comments_can_be_kept(monkeypatch, tmp_path):
    old_file = tmp_path / "old.py"
    new_file = tmp_path / "new.py"
    old_file.write_text("old = True\n", encoding="utf-8")
    new_file.write_text("new = True\n", encoding="utf-8")
    old_issue = make_issue(sonar_key="OLD", file=str(old_file), line=1)
    new_issue = make_issue(sonar_key="NEW", file=str(new_file), line=1)
    resolved: list[int] = []
    deleted: list[int] = []

    async def fake_get_pull_request_comments(*_args):
        return [existing_comment(old_issue, 10)]

    async def fake_resolve_comment_ids(_pr_id, _repo_slug, _workspace, comment_ids):
        resolved.extend(sorted(comment_ids))
        return set(comment_ids), set()

    async def fake_delete_comment_ids(_pr_id, _repo_slug, _workspace, comment_ids):
        deleted.extend(sorted(comment_ids))
        return set(comment_ids), set()

    async def fake_post_issue_group_comment(*_args, **_kwargs):
        return True

    monkeypatch.setenv("CODEGUARDIAN_COMMENT_SYNC_MODE", "keep")
    monkeypatch.setattr("codeguardian.bitbucket.get_pull_request_comments", fake_get_pull_request_comments)
    monkeypatch.setattr("codeguardian.bitbucket.resolve_comment_ids", fake_resolve_comment_ids)
    monkeypatch.setattr("codeguardian.bitbucket.delete_comment_ids", fake_delete_comment_ids)
    monkeypatch.setattr("codeguardian.bitbucket.post_issue_group_comment", fake_post_issue_group_comment)

    result = await synchronize_inline_comments(None, "7", "repo", "workspace", [new_issue])

    assert resolved == []
    assert deleted == []
    assert result.resolved == 0
    assert result.deleted == 0
    assert result.kept == 1


@pytest.mark.asyncio
async def test_human_comments_are_ignored_during_obsolete_cleanup(monkeypatch, tmp_path):
    issue_file = tmp_path / "new.py"
    issue_file.write_text("new = True\n", encoding="utf-8")
    issue = make_issue(sonar_key="NEW", file=str(issue_file), line=1)
    resolved: list[int] = []
    deleted: list[int] = []

    async def fake_get_pull_request_comments(*_args):
        return [{
            "id": 99,
            "content": {"raw": "human review comment"},
            "inline": {"path": str(issue_file), "to": 1},
        }]

    async def fake_resolve_comment_ids(_pr_id, _repo_slug, _workspace, comment_ids):
        resolved.extend(sorted(comment_ids))
        return set(comment_ids), set()

    async def fake_delete_comment_ids(_pr_id, _repo_slug, _workspace, comment_ids):
        deleted.extend(sorted(comment_ids))
        return set(comment_ids), set()

    async def fake_post_issue_group_comment(*_args, **_kwargs):
        return True

    monkeypatch.setattr("codeguardian.bitbucket.get_pull_request_comments", fake_get_pull_request_comments)
    monkeypatch.setattr("codeguardian.bitbucket.resolve_comment_ids", fake_resolve_comment_ids)
    monkeypatch.setattr("codeguardian.bitbucket.delete_comment_ids", fake_delete_comment_ids)
    monkeypatch.setattr("codeguardian.bitbucket.post_issue_group_comment", fake_post_issue_group_comment)

    result = await synchronize_inline_comments(None, "7", "repo", "workspace", [issue])

    assert resolved == []
    assert deleted == []
    assert result.created == 1


@pytest.mark.asyncio
async def test_resolved_codeguardian_comments_are_not_reused_or_resolved_again(monkeypatch, tmp_path):
    issue_file = tmp_path / "service.py"
    issue_file.write_text("value = 1\n", encoding="utf-8")
    issue = make_issue(file=str(issue_file), line=1)
    resolved: list[int] = []
    created = 0

    async def fake_get_pull_request_comments(*_args):
        comment = existing_comment(issue, 10)
        comment["state"] = "resolved"
        return [comment]

    async def fake_resolve_comment_ids(_pr_id, _repo_slug, _workspace, comment_ids):
        resolved.extend(sorted(comment_ids))
        return set(comment_ids), set()

    async def fake_post_issue_group_comment(*_args, **_kwargs):
        nonlocal created
        created += 1
        return True

    monkeypatch.setattr("codeguardian.bitbucket.get_pull_request_comments", fake_get_pull_request_comments)
    monkeypatch.setattr("codeguardian.bitbucket.resolve_comment_ids", fake_resolve_comment_ids)
    monkeypatch.setattr("codeguardian.bitbucket.post_issue_group_comment", fake_post_issue_group_comment)

    result = await synchronize_inline_comments(None, "7", "repo", "workspace", [issue])

    assert resolved == []
    assert created == 1
    assert result.created == 1
    assert result.reused == 0
