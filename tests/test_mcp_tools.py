import httpx

from codeguardian import mcp_tools
from codeguardian.comments import hidden_ids
from codeguardian.config import get_atlassian_mcp_auth
from codeguardian.config import CODEGUARDIAN_AGENT_MARKER


def test_list_sonarqube_findings_normalizes_response(monkeypatch):
    def handler(request):
        assert request.url.path == "/api/issues/search"
        return httpx.Response(200, json={
            "total": 1,
            "issues": [{
                "key": "S1",
                "severity": "CRITICAL",
                "type": "VULNERABILITY",
                "message": "SQL injection",
                "component": "sample:app.py",
                "textRange": {"startLine": 12},
                "rule": "python:S3649",
                "status": "OPEN",
            }],
        })

    monkeypatch.setenv("SONARQUBE_AUTH_TOKEN", "token")
    monkeypatch.setenv("SONARQUBE_URL", "http://sonarqube.local")
    monkeypatch.setattr(mcp_tools, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))

    result = mcp_tools.list_sonarqube_findings("sample", severities="CRITICAL")

    assert result["total"] == 1
    assert result["issues"][0]["key"] == "S1"
    assert result["issues"][0]["line"] == 12


def test_list_bitbucket_codeguardian_comments_extracts_agent_comments(monkeypatch):
    body = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "### CodeGuardian optimization suggestion\n\n"
        f"{hidden_ids(['OPTIMIZATION:abc'])}"
    )

    async def fake_comments(workspace, repo_slug, pr_id):
        return [{
            "id": 10,
            "content": {"raw": body},
            "inline": {"path": "app.py", "to": 4},
            "deleted": False,
        }, {
            "id": 11,
            "content": {"raw": "human comment"},
            "inline": {"path": "app.py", "to": 5},
            "deleted": False,
        }]

    monkeypatch.setattr(mcp_tools, "fetch_atlassian_pr_comments", fake_comments)

    result = mcp_tools.list_bitbucket_codeguardian_comments("ws", "repo", 7)

    assert result["total_codeguardian_comments"] == 1
    assert result["comments"][0]["is_optimization"] is True
    assert result["comments"][0]["file"] == "app.py"


def test_repo_slug_alias_is_normalized():
    assert mcp_tools.normalize_repo_slug("codeguardian-sample-mixed") == "sample-mixed"
    assert mcp_tools.normalize_repo_slug("sample-mixed") == "sample-mixed"


def test_atlassian_mcp_auth_accepts_raw_basic_base64(monkeypatch):
    monkeypatch.setenv("ATLASSIAN_MCP_AUTH_HEADER", "dXNlcjp0b2tlbg==")

    auth = get_atlassian_mcp_auth()

    assert isinstance(auth, httpx.BasicAuth)


def test_list_codeguardian_comment_summaries_returns_all_compact_comments(monkeypatch):
    body = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "### CodeGuardian optimization suggestion\n\n"
        "**Optimization opportunity:**\n\nRepeated scan.\n\n"
        "**Suggested optimization:**\n\nUse a set.\n\n"
        "**Block to substitute:**\n"
        "```python\n"
        "return item in values\n"
        "```\n\n"
        "**Proposed Code:**\n"
        "```python\n"
        "return item in values_set\n"
        "```\n\n"
        f"{hidden_ids(['OPTIMIZATION:abc'])}"
    )

    async def fake_comments(workspace, repo_slug, pr_id):
        return [{
            "id": 10,
            "content": {"raw": body},
            "inline": {"path": "app.py", "to": 4},
            "deleted": False,
        }, {
            "id": 12,
            "content": {"raw": body.replace("OPTIMIZATION:abc", "S1")},
            "inline": {"path": "service.py", "to": 8},
            "deleted": False,
        }]

    monkeypatch.setattr(mcp_tools, "fetch_atlassian_pr_comments", fake_comments)

    result = mcp_tools.list_codeguardian_comment_summaries("ws", "repo", 7)

    assert result["total"] == 2
    assert result["comments"][0]["original_code"] == "return item in values"
    assert result["comments"][0]["proposed_code"] == "return item in values_set"
    assert result["comments"][0]["has_code_blocks"] is True
    assert "original_code" in result["display_instruction"]
    assert "Apply change:" not in result["markdown"]
    assert "## Suggestion 1" in result["markdown"]
    assert "Comment ID: `10`" in result["markdown"]
    assert "File: app.py" in result["markdown"]
    assert "Original code:" in result["markdown"]
    assert "Explanation:" in result["markdown"]
    assert "Proposed code:" in result["markdown"]
    assert result["markdown"].index("Original code:") < result["markdown"].index("Explanation:")
    assert result["markdown"].index("Explanation:") < result["markdown"].index("Proposed code:")


def test_get_pr_review_status_includes_markdown_comment_summaries(monkeypatch):
    body = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Optimization opportunity:**\n\nRepeated scan.\n\n"
        "**Suggested optimization:**\n\nUse a set.\n\n"
        "**Block to substitute:**\n"
        "```python\n"
        "return item in values\n"
        "```\n\n"
        "**Proposed Code:**\n"
        "```python\n"
        "return item in values_set\n"
        "```\n\n"
        f"{hidden_ids(['OPTIMIZATION:abc'])}"
    )

    async def fake_call(action, arguments):
        assert action == "get"
        return {
            "title": "Demo PR",
            "state": "OPEN",
            "source": {"branch": {"name": "demo"}},
            "destination": {"branch": {"name": "main"}},
        }

    async def fake_comments(workspace, repo_slug, pr_id):
        return [{
            "id": 10,
            "content": {"raw": body},
            "inline": {"path": "app.py", "to": 4},
            "deleted": False,
        }]

    monkeypatch.setattr(mcp_tools, "_call_bitbucket_pull_request", fake_call)
    monkeypatch.setattr(mcp_tools, "fetch_atlassian_pr_comments", fake_comments)

    result = mcp_tools.get_pr_review_status("ws", "repo", 7)

    assert result["codeguardian_comments"] == 1
    assert result["optimization_comments"] == 1
    assert result["comments_tool"] == "Use list_codeguardian_comments to see every comment with code blocks."


def test_list_comments_for_open_pr_uses_single_open_pr(monkeypatch):
    body = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Optimization opportunity:**\n\nRepeated scan.\n\n"
        "**Suggested optimization:**\n\nUse a set.\n\n"
        "**Block to substitute:**\n```python\nreturn x\n```\n\n"
        "**Proposed Code:**\n```python\nreturn y\n```\n\n"
        f"{hidden_ids(['OPTIMIZATION:abc'])}"
    )

    async def fake_call(action, arguments):
        assert action == "list"
        return {
            "values": [{
                "id": 1,
                "title": "Demo PR",
                "state": "OPEN",
                "source": {"branch": {"name": "demo"}},
                "destination": {"branch": {"name": "main"}},
            }],
        }

    async def fake_comments(workspace, repo_slug, pr_id):
        return [{
            "id": 10,
            "content": {"raw": body},
            "inline": {"path": "app.py", "to": 4},
            "deleted": False,
        }]

    monkeypatch.setattr(mcp_tools, "_call_bitbucket_pull_request", fake_call)
    monkeypatch.setattr(mcp_tools, "fetch_atlassian_pr_comments", fake_comments)

    result = mcp_tools.list_comments_for_open_pr("ws", "codeguardian-sample-mixed")

    assert "# CodeGuardian review suggestions for ws/sample-mixed PR 1" in result
    assert "Showing suggestion 1 of 1" in result
    assert "Comment ID: `10`" in result


def test_list_comments_for_open_pr_detects_repo_from_git_remote(monkeypatch, tmp_path):
    repo = tmp_path / "codeguardian-sample-mixed"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        "[remote \"origin\"]\n"
        "    url = https://bitbucket.org/medinafdzz/codeguardian-sample-mixed.git\n",
        encoding="utf-8",
    )

    body = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Optimization opportunity:**\n\nRepeated scan.\n\n"
        "**Suggested optimization:**\n\nUse a set.\n\n"
        "**Block to substitute:**\n```python\nreturn x\n```\n\n"
        "**Proposed Code:**\n```python\nreturn y\n```\n\n"
        f"{hidden_ids(['OPTIMIZATION:abc'])}"
    )

    async def fake_call(action, arguments):
        assert action == "list"
        assert arguments["workspaceId"] == "medinafdzz"
        assert arguments["repoId"] == "sample-mixed"
        return {
            "values": [{
                "id": 1,
                "title": "Detected PR",
                "state": "OPEN",
                "source": {"branch": {"name": "demo"}},
                "destination": {"branch": {"name": "main"}},
            }],
        }

    async def fake_comments(workspace, repo_slug, pr_id):
        assert workspace == "medinafdzz"
        assert repo_slug == "sample-mixed"
        return [{
            "id": 10,
            "content": {"raw": body},
            "inline": {"path": "app.py", "to": 4},
            "deleted": False,
        }]

    monkeypatch.setenv("CODEGUARDIAN_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("BITBUCKET_REPO_SLUG", raising=False)
    monkeypatch.delenv("CODEGUARDIAN_DEFAULT_REPO_SLUG", raising=False)
    monkeypatch.setattr(mcp_tools, "_call_bitbucket_pull_request", fake_call)
    monkeypatch.setattr(mcp_tools, "fetch_atlassian_pr_comments", fake_comments)

    result = mcp_tools.list_comments_for_open_pr("", "")

    assert "# CodeGuardian review suggestions for medinafdzz/sample-mixed PR 1" in result
    assert "Showing suggestion 1 of 1" in result
    assert "Comment ID: `10`" in result


def test_list_comments_for_open_pr_asks_when_multiple_open_prs(monkeypatch):
    async def fake_call(action, arguments):
        return {
            "values": [{
                "id": 1,
                "title": "First PR",
                "state": "OPEN",
                "source": {"branch": {"name": "demo-1"}},
                "destination": {"branch": {"name": "main"}},
            }, {
                "id": 2,
                "title": "Second PR",
                "state": "OPEN",
                "source": {"branch": {"name": "demo-2"}},
                "destination": {"branch": {"name": "main"}},
            }],
        }

    monkeypatch.setattr(mcp_tools, "_call_bitbucket_pull_request", fake_call)

    result = mcp_tools.list_comments_for_open_pr("ws", "sample-mixed")

    assert "Multiple open pull requests found" in result
    assert "PR 1: First PR" in result
    assert "PR 2: Second PR" in result


def test_get_codeguardian_metrics_queries_prometheus(monkeypatch):
    requested_queries = []

    def handler(request):
        requested_queries.append(str(request.url.params.get("query")))
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus.local")
    monkeypatch.setattr(mcp_tools, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))

    result = mcp_tools.get_codeguardian_metrics("sample")

    assert result["repository"] == "sample"
    assert any(query == 'codeguardian_sonar_findings_total{repository="sample"}' for query in requested_queries)


def test_get_jenkins_build_summary_returns_core_fields(monkeypatch):
    def handler(request):
        assert request.url.path == "/job/folder/job/demo/lastBuild/api/json"
        return httpx.Response(200, json={
            "number": 42,
            "result": "SUCCESS",
            "building": False,
            "duration": 12000,
            "timestamp": 123,
            "url": "http://jenkins/job/demo/42/",
            "fullDisplayName": "folder/demo #42",
            "changeSet": {"items": [{"msg": "change"}]},
        })

    monkeypatch.setenv("JENKINS_URL", "http://jenkins.local")
    monkeypatch.setattr(mcp_tools, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))

    result = mcp_tools.get_jenkins_build_summary("folder/demo")

    assert result["build_number"] == 42
    assert result["result"] == "SUCCESS"
    assert result["changes"] == [{"msg": "change"}]


def test_apply_codeguardian_comment_replacement_applies_immediately(monkeypatch, tmp_path):
    repo = tmp_path / "codeguardian-sample"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def run():\n    return item in values\n", encoding="utf-8")

    body = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Block to substitute:**\n"
        "```python\n"
        "return item in values\n"
        "```\n\n"
        "**Proposed Code:**\n"
        "```python\n"
        "return item in values_set\n"
        "```\n\n"
        f"{hidden_ids(['OPTIMIZATION:abc'])}"
    )

    async def fake_comments(workspace, repo_slug, pr_id):
        return [{
            "id": 10,
            "content": {"raw": body},
            "inline": {"path": "app.py", "to": 2},
        }]

    monkeypatch.setenv("CODEGUARDIAN_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_tools, "fetch_atlassian_pr_comments", fake_comments)

    applied = mcp_tools.apply_codeguardian_comment_replacement("ws", "sample", 7, 10)

    assert applied["applied"] is True
    assert source.read_text(encoding="utf-8") == "def run():\n    return item in values_set\n"


def test_review_codeguardian_suggestions_returns_acceptance_prompt(monkeypatch):
    body = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Optimization opportunity:**\n\nRepeated scan.\n\n"
        "**Suggested optimization:**\n\nUse a set.\n\n"
        "**Block to substitute:**\n```python\nreturn x\n```\n\n"
        "**Proposed Code:**\n```python\nreturn y\n```\n\n"
        f"{hidden_ids(['OPTIMIZATION:abc'])}"
    )

    async def fake_comments(workspace, repo_slug, pr_id):
        return [{
            "id": 10,
            "content": {"raw": body},
            "inline": {"path": "app.py", "to": 2},
            "deleted": False,
        }]

    monkeypatch.setattr(mcp_tools, "fetch_atlassian_pr_comments", fake_comments)

    result = mcp_tools.review_codeguardian_suggestions("ws", "sample", 7)

    assert "## Suggestion 1" in result
    assert "Requested start=1, count=1." in result
    assert "Returned suggestion indices=[1]." in result
    assert "Returned comment ids=[10]." in result
    assert "Showing suggestion 1 of 1" in result
    assert "Original code:" in result
    assert "Explanation:" in result
    assert "Proposed code:" in result
    assert "Do not summarize it" in result
    assert "whether to accept or skip suggestion 1" in result
    assert "Apply change:" not in result


def test_review_codeguardian_suggestions_pages_one_suggestion_by_default(monkeypatch):
    body_one = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Optimization opportunity:**\n\nFirst.\n\n"
        "**Suggested optimization:**\n\nUse y.\n\n"
        "**Block to substitute:**\n```python\nreturn x\n```\n\n"
        "**Proposed Code:**\n```python\nreturn y\n```\n\n"
        f"{hidden_ids(['OPTIMIZATION:one'])}"
    )
    body_two = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Optimization opportunity:**\n\nSecond.\n\n"
        "**Suggested optimization:**\n\nUse b.\n\n"
        "**Block to substitute:**\n```python\nreturn a\n```\n\n"
        "**Proposed Code:**\n```python\nreturn b\n```\n\n"
        f"{hidden_ids(['OPTIMIZATION:two'])}"
    )

    async def fake_comments(workspace, repo_slug, pr_id):
        return [{
            "id": 10,
            "content": {"raw": body_one},
            "inline": {"path": "one.py", "to": 2},
            "deleted": False,
        }, {
            "id": 11,
            "content": {"raw": body_two},
            "inline": {"path": "two.py", "to": 2},
            "deleted": False,
        }]

    monkeypatch.setattr(mcp_tools, "fetch_atlassian_pr_comments", fake_comments)

    first = mcp_tools.review_codeguardian_suggestions("ws", "sample", 7)
    second = mcp_tools.review_codeguardian_suggestions("ws", "sample", 7, start=2)

    assert "Showing suggestion 1 of 2" in first
    assert "Requested start=1, count=1." in first
    assert "Returned suggestion indices=[1]." in first
    assert "Returned comment ids=[10]." in first
    assert "File: one.py" in first
    assert "File: two.py" not in first
    assert "start=2" in first
    assert "Showing suggestion 2 of 2" in second
    assert "Requested start=2, count=1." in second
    assert "Returned suggestion indices=[2]." in second
    assert "Returned comment ids=[11]." in second
    assert "File: two.py" in second


def test_apply_approved_codeguardian_suggestions_applies_selected_indices(monkeypatch, tmp_path):
    repo = tmp_path / "codeguardian-sample"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def one():\n    return x\n\ndef two():\n    return a\n", encoding="utf-8")

    body_one = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Block to substitute:**\n```python\nreturn x\n```\n\n"
        "**Proposed Code:**\n```python\nreturn y\n```\n\n"
        f"{hidden_ids(['OPTIMIZATION:one'])}"
    )
    body_two = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Block to substitute:**\n```python\nreturn a\n```\n\n"
        "**Proposed Code:**\n```python\nreturn b\n```\n\n"
        f"{hidden_ids(['OPTIMIZATION:two'])}"
    )

    async def fake_comments(workspace, repo_slug, pr_id):
        return [{
            "id": 10,
            "content": {"raw": body_one},
            "inline": {"path": "app.py", "to": 2},
            "deleted": False,
        }, {
            "id": 11,
            "content": {"raw": body_two},
            "inline": {"path": "app.py", "to": 5},
            "deleted": False,
        }]

    monkeypatch.setenv("CODEGUARDIAN_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_tools, "fetch_atlassian_pr_comments", fake_comments)

    result = mcp_tools.apply_approved_codeguardian_suggestions("2", "ws", "sample", 7)

    assert result["applied_count"] == 1
    assert result["results"][0]["suggestion_index"] == 2
    assert source.read_text(encoding="utf-8") == "def one():\n    return x\n\ndef two():\n    return b\n"


def test_apply_approved_codeguardian_suggestions_accepts_raw_comment_id(monkeypatch, tmp_path):
    repo = tmp_path / "codeguardian-sample"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def run():\n    return x\n", encoding="utf-8")

    body = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Block to substitute:**\n```python\nreturn x\n```\n\n"
        "**Proposed Code:**\n```python\nreturn y\n```\n\n"
        f"{hidden_ids(['OPTIMIZATION:one'])}"
    )

    async def fake_comments(workspace, repo_slug, pr_id):
        return [{
            "id": 796961607,
            "content": {"raw": body},
            "inline": {"path": "app.py", "to": 2},
            "deleted": False,
        }]

    monkeypatch.setenv("CODEGUARDIAN_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_tools, "fetch_atlassian_pr_comments", fake_comments)

    result = mcp_tools.apply_approved_codeguardian_suggestions("796961607", "ws", "sample", 7)

    assert result["applied_count"] == 1
    assert result["results"][0]["comment_id"] == 796961607
    assert source.read_text(encoding="utf-8") == "def run():\n    return y\n"


def test_codeguardian_batch_filters_type_and_skips_solved(monkeypatch, tmp_path):
    repo = tmp_path / "codeguardian-sample"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def sonar():\n    return insecure\n\ndef opt():\n    return slow\n", encoding="utf-8")

    sonar_body = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Problems:**\n\nSecurity issue.\n\n"
        "**Solutions:**\n\nUse safe value.\n\n"
        "**Block to substitute:**\n```python\nreturn insecure\n```\n\n"
        "**Proposed Code:**\n```python\nreturn safe\n```\n\n"
        f"{hidden_ids(['SONAR:1'])}"
    )
    optimization_body = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Optimization opportunity:**\n\nSlow lookup.\n\n"
        "**Suggested optimization:**\n\nUse fast lookup.\n\n"
        "**Block to substitute:**\n```python\nreturn slow\n```\n\n"
        "**Proposed Code:**\n```python\nreturn fast\n```\n\n"
        f"{hidden_ids(['OPTIMIZATION:1'])}"
    )
    solved_body = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Optimization opportunity:**\n\nAlready solved.\n\n"
        "**Suggested optimization:**\n\nUse new code.\n\n"
        "**Block to substitute:**\n```python\nreturn old_missing\n```\n\n"
        "**Proposed Code:**\n```python\nreturn new_code\n```\n\n"
        f"{hidden_ids(['OPTIMIZATION:2'])}"
    )

    async def fake_comments(workspace, repo_slug, pr_id):
        return [{
            "id": 10,
            "content": {"raw": sonar_body},
            "inline": {"path": "app.py", "to": 2},
            "deleted": False,
        }, {
            "id": 11,
            "content": {"raw": optimization_body},
            "inline": {"path": "app.py", "to": 5},
            "deleted": False,
        }, {
            "id": 12,
            "content": {"raw": solved_body},
            "inline": {"path": "app.py", "to": 8},
            "deleted": False,
        }]

    monkeypatch.setenv("CODEGUARDIAN_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_tools, "fetch_atlassian_pr_comments", fake_comments)

    review = mcp_tools.codeguardian_batch("review", "ws", "sample", 7)
    improvements = mcp_tools.codeguardian_batch("improvements", "ws", "sample", 7)

    assert "# Code review" in review
    assert "Security issue." in review
    assert "Slow lookup." not in review
    assert "# Code improvements" in improvements
    assert "Slow lookup." in improvements
    assert "Already solved." not in improvements


def test_apply_codeguardian_batch_selection_uses_visible_numbers(monkeypatch, tmp_path):
    repo = tmp_path / "codeguardian-sample"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def one():\n    return a\n\ndef two():\n    return c\n", encoding="utf-8")

    body_one = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Optimization opportunity:**\n\nFirst.\n\n"
        "**Suggested optimization:**\n\nUse b.\n\n"
        "**Block to substitute:**\n```python\nreturn a\n```\n\n"
        "**Proposed Code:**\n```python\nreturn b\n```\n\n"
        f"{hidden_ids(['OPTIMIZATION:1'])}"
    )
    body_two = (
        f"{CODEGUARDIAN_AGENT_MARKER}\n"
        "**Optimization opportunity:**\n\nSecond.\n\n"
        "**Suggested optimization:**\n\nUse d.\n\n"
        "**Block to substitute:**\n```python\nreturn c\n```\n\n"
        "**Proposed Code:**\n```python\nreturn d\n```\n\n"
        f"{hidden_ids(['OPTIMIZATION:2'])}"
    )

    async def fake_comments(workspace, repo_slug, pr_id):
        return [{
            "id": 20,
            "content": {"raw": body_one},
            "inline": {"path": "app.py", "to": 2},
            "deleted": False,
        }, {
            "id": 21,
            "content": {"raw": body_two},
            "inline": {"path": "app.py", "to": 5},
            "deleted": False,
        }]

    monkeypatch.setenv("CODEGUARDIAN_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_tools, "fetch_atlassian_pr_comments", fake_comments)

    result = mcp_tools.apply_codeguardian_batch_selection("improvements", "1 and 2", "ws", "sample", 7)

    assert result["applied_count"] == 2
    assert [item["selection_number"] for item in result["results"]] == [1, 2]
    assert source.read_text(encoding="utf-8") == "def one():\n    return b\n\ndef two():\n    return d\n"
