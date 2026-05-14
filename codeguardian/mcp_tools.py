import asyncio
import base64
import configparser
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from codeguardian.atlassian import atlassian_rovo_session
from codeguardian.bitbucket import get_pull_request_comments
from codeguardian.comments import extract_issue_key, is_agent_comment
from codeguardian.text import normalize_code_block


DEFAULT_TIMEOUT = 20.0


def _clean_base_url(value: str, default: str) -> str:
    return (value or default).rstrip("/")


def _basic_auth_header(username: str, token: str) -> dict[str, str]:
    raw = f"{username}:{token}".encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


def sonarqube_headers() -> dict[str, str]:
    token = (os.getenv("SONARQUBE_AUTH_TOKEN") or os.getenv("SONAR_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Missing SONARQUBE_AUTH_TOKEN")
    return {"Accept": "application/json", "Authorization": f"Bearer {token}"}


def jenkins_headers() -> dict[str, str]:
    username = (os.getenv("JENKINS_USER") or os.getenv("JENKINS_USERNAME") or "").strip()
    token = (os.getenv("JENKINS_API_TOKEN") or os.getenv("JENKINS_TOKEN") or "").strip()
    if not username or not token:
        return {"Accept": "application/json"}
    return {"Accept": "application/json", **_basic_auth_header(username, token)}


def _client() -> httpx.Client:
    return httpx.Client(timeout=DEFAULT_TIMEOUT)


def _get_json(url: str, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with _client() as client:
        response = client.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected JSON response")
    return data


def _sonarqube_base_url() -> str:
    return _clean_base_url(os.getenv("SONARQUBE_URL", ""), "http://sonarqube-server:9000")


def _prometheus_base_url() -> str:
    return _clean_base_url(os.getenv("PROMETHEUS_URL", ""), "http://prometheus:9090")


def _jenkins_base_url() -> str:
    return _clean_base_url(os.getenv("JENKINS_URL", ""), "http://jenkins-blueocean:8080")


def normalize_repo_slug(repo_slug: str) -> str:
    normalized = repo_slug.strip().removesuffix(".git")
    if normalized.startswith("codeguardian-"):
        candidate = normalized.removeprefix("codeguardian-")
        if candidate.startswith("sample-"):
            return candidate
    return normalized


def _resolve_workspace(workspace: str = "") -> str:
    resolved = (
        workspace.strip()
        or os.getenv("BITBUCKET_WORKSPACE", "").strip()
    )
    if not resolved:
        raise RuntimeError("Missing workspace. Provide workspace or set BITBUCKET_WORKSPACE.")
    return resolved


def _resolve_repo_slug(repo_slug: str = "") -> str:
    resolved = repo_slug.strip()
    if not resolved:
        raise RuntimeError("Missing repo_slug. Provide repo_slug or use the open-PR discovery tool.")
    return normalize_repo_slug(resolved)


def _parse_bitbucket_remote(remote_url: str) -> tuple[str, str] | None:
    patterns = [
        r"bitbucket\.org[:/](?P<workspace>[^/]+)/(?P<repo>[^/\s]+?)(?:\.git)?$",
        r"bitbucket\.org/(?P<workspace>[^/]+)/(?P<repo>[^/\s]+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, remote_url.strip())
        if match:
            return match.group("workspace"), normalize_repo_slug(match.group("repo"))
    return None


def _read_origin_remote(repo_path: Path) -> str:
    config_path = repo_path / ".git" / "config"
    if not config_path.is_file():
        return ""
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    section = 'remote "origin"'
    if parser.has_option(section, "url"):
        return parser.get(section, "url")
    return ""


def _local_git_repositories() -> list[Path]:
    root = _workspace_root()
    candidates = [root]
    if root.is_dir():
        candidates.extend(path for path in root.iterdir() if path.is_dir())
    return [path for path in candidates if (path / ".git").exists()]


def _detect_bitbucket_repositories(workspace: str = "") -> list[dict[str, str]]:
    explicit_workspace = workspace.strip()
    repositories = []
    seen = set()
    for repo_path in _local_git_repositories():
        parsed = _parse_bitbucket_remote(_read_origin_remote(repo_path))
        if parsed:
            detected_workspace, repo_slug = parsed
        elif explicit_workspace:
            detected_workspace = explicit_workspace
            repo_slug = normalize_repo_slug(repo_path.name)
        else:
            continue

        if explicit_workspace and detected_workspace != explicit_workspace:
            continue

        key = (detected_workspace, repo_slug)
        if key in seen:
            continue
        seen.add(key)
        repositories.append({
            "workspace": detected_workspace,
            "repo_slug": repo_slug,
            "local_path": str(repo_path),
        })
    return repositories


def _run_async(coro):
    return asyncio.run(coro)


def _parse_mcp_tool_json(result: Any) -> Any:
    text = result.content[0].text
    return json.loads(text)


async def _call_bitbucket_pull_request(action: str, arguments: dict[str, Any]) -> Any:
    async with atlassian_rovo_session() as session:
        result = await session.call_tool(
            name="bitbucketPullRequest",
            arguments={"action": action, **arguments},
        )
        return _parse_mcp_tool_json(result)


async def fetch_atlassian_pr_comments(workspace: str, repo_slug: str, pr_id: int) -> list[dict]:
    async with atlassian_rovo_session() as session:
        return await get_pull_request_comments(session, str(pr_id), repo_slug, workspace)


def _extract_values(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        values = data.get("values") or data.get("pullrequests") or data.get("pull_requests") or data.get("comments")
        if isinstance(values, list):
            return [item for item in values if isinstance(item, dict)]
        if data.get("id") is not None:
            return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


async def list_open_pull_requests_async(workspace: str, repo_slug: str) -> dict[str, Any]:
    workspace = _resolve_workspace(workspace)
    normalized_repo_slug = _resolve_repo_slug(repo_slug)
    data = await _call_bitbucket_pull_request(
        "list",
        {
            "workspaceId": workspace,
            "repoId": normalized_repo_slug,
            "state": "OPEN",
            "pagelen": 50,
            "sort": "-updated_on",
        },
    )
    pull_requests = _extract_values(data)
    normalized_prs = []
    for pr in pull_requests:
        normalized_prs.append({
            "id": pr.get("id"),
            "title": pr.get("title"),
            "state": pr.get("state"),
            "source_branch": ((pr.get("source") or {}).get("branch") or {}).get("name"),
            "destination_branch": ((pr.get("destination") or {}).get("branch") or {}).get("name"),
            "updated_on": pr.get("updated_on"),
        })
    return {
        "workspace": workspace,
        "repo_slug": normalized_repo_slug,
        "total_open_pull_requests": len(normalized_prs),
        "pull_requests": normalized_prs,
    }


def list_open_pull_requests(workspace: str, repo_slug: str) -> dict[str, Any]:
    return _run_async(list_open_pull_requests_async(workspace, repo_slug))


async def list_comments_for_open_pr_async(workspace: str, repo_slug: str) -> str:
    if not repo_slug.strip():
        return await list_comments_for_detected_open_pr_async(workspace)

    workspace = _resolve_workspace(workspace)
    repo_slug = _resolve_repo_slug(repo_slug)
    prs = await list_open_pull_requests_async(workspace, repo_slug)
    open_prs = prs["pull_requests"]

    if not open_prs:
        return (
            f"No open pull requests found for repository `{prs['repo_slug']}` "
            f"in workspace `{workspace}`."
        )

    if len(open_prs) > 1:
        lines = [
            f"Multiple open pull requests found for `{prs['repo_slug']}`.",
            "Please choose one PR and ask for its comments:",
            "",
        ]
        for pr in open_prs:
            lines.append(
                f"- PR {pr['id']}: {pr['title']} "
                f"({pr['source_branch']} -> {pr['destination_branch']})"
            )
        return "\n".join(lines)

    pr_id = int(open_prs[0]["id"])
    comments = await list_bitbucket_codeguardian_comments_async(workspace, prs["repo_slug"], pr_id)
    return (
        f"Open PR selected: PR {pr_id} - {open_prs[0]['title']}\n\n"
        f"{comments['markdown']}"
    )


def list_comments_for_open_pr(workspace: str, repo_slug: str) -> str:
    return _run_async(list_comments_for_open_pr_async(workspace, repo_slug))


async def list_comments_for_detected_open_pr_async(workspace: str = "") -> str:
    repositories = _detect_bitbucket_repositories(workspace)
    if not repositories:
        return (
            "No Bitbucket repository could be detected from the mounted local workspace. "
            "Open a repository with a Bitbucket origin remote or provide workspace and repo_slug explicitly."
        )

    open_matches = []
    lookup_errors = []
    for repository in repositories:
        try:
            prs = await list_open_pull_requests_async(repository["workspace"], repository["repo_slug"])
        except Exception as exc:
            lookup_errors.append(
                f"- {repository['workspace']}/{repository['repo_slug']}: {exc}"
            )
            continue

        for pr in prs["pull_requests"]:
            open_matches.append({
                **pr,
                "workspace": repository["workspace"],
                "repo_slug": repository["repo_slug"],
            })

    if not open_matches:
        lines = ["No open pull requests found in detected Bitbucket repositories."]
        if lookup_errors:
            lines.extend(["", "Repositories that could not be checked:"])
            lines.extend(lookup_errors)
        return "\n".join(lines)

    if len(open_matches) > 1:
        lines = [
            "Multiple open pull requests found.",
            "Please choose one PR and ask for its comments:",
            "",
        ]
        for pr in open_matches:
            lines.append(
                f"- {pr['workspace']}/{pr['repo_slug']} PR {pr['id']}: {pr['title']} "
                f"({pr['source_branch']} -> {pr['destination_branch']})"
            )
        return "\n".join(lines)

    pr = open_matches[0]
    comments = await list_bitbucket_codeguardian_comments_async(
        pr["workspace"],
        pr["repo_slug"],
        int(pr["id"]),
    )
    return (
        f"Open PR selected: {pr['workspace']}/{pr['repo_slug']} "
        f"PR {pr['id']} - {pr['title']}\n\n"
        f"{comments['markdown']}"
    )


def list_comments_for_detected_open_pr(workspace: str = "") -> str:
    return _run_async(list_comments_for_detected_open_pr_async(workspace))


def list_sonarqube_findings(
    project_key: str,
    severities: str = "",
    resolved: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "componentKeys": project_key,
        "resolved": str(resolved).lower(),
        "ps": max(1, min(limit, 500)),
    }
    if severities.strip():
        params["severities"] = severities.strip()

    data = _get_json(
        f"{_sonarqube_base_url()}/api/issues/search",
        headers=sonarqube_headers(),
        params=params,
    )
    issues = data.get("issues", [])
    if not isinstance(issues, list):
        issues = []

    normalized = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        normalized.append({
            "key": issue.get("key"),
            "severity": issue.get("severity"),
            "type": issue.get("type"),
            "message": issue.get("message"),
            "component": issue.get("component"),
            "line": (issue.get("textRange") or {}).get("startLine"),
            "rule": issue.get("rule"),
            "status": issue.get("status"),
        })

    return {
        "project_key": project_key,
        "total": data.get("total", len(normalized)),
        "returned": len(normalized),
        "issues": normalized,
    }


async def list_bitbucket_codeguardian_comments_async(
    workspace: str,
    repo_slug: str,
    pr_id: int,
    limit: int = 100,
) -> dict[str, Any]:
    workspace = _resolve_workspace(workspace)
    normalized_repo_slug = _resolve_repo_slug(repo_slug)
    comments = (await fetch_atlassian_pr_comments(workspace, normalized_repo_slug, pr_id))[:limit]

    agent_comments = []
    for comment in comments:
        if not isinstance(comment, dict) or comment.get("deleted"):
            continue
        raw = (comment.get("content") or {}).get("raw", "")
        if not is_agent_comment(raw):
            continue
        inline = comment.get("inline") or {}
        issue_keys = extract_issue_key(raw)
        agent_comments.append({
            "id": comment.get("id"),
            "created_on": comment.get("created_on"),
            "updated_on": comment.get("updated_on"),
            "file": inline.get("path"),
            "line": inline.get("to") or inline.get("from"),
            "issue_keys": issue_keys,
            "is_optimization": any(str(key).startswith("OPTIMIZATION:") for key in issue_keys),
            "is_sonarqube": any(not str(key).startswith(("OPTIMIZATION:", "PERFORMANCE:")) for key in issue_keys),
            "body": raw,
        })

    summaries = [
        _comment_summary(comment, workspace, repo_slug, pr_id, index)
        for index, comment in enumerate(agent_comments, start=1)
    ]
    markdown = "\n\n---\n\n".join(summary["markdown"] for summary in summaries)

    return {
        "workspace": workspace,
        "repo_slug": normalized_repo_slug,
        "pr_id": pr_id,
        "total_codeguardian_comments": len(agent_comments),
        "display_instruction": (
            "Answer in English. Show every comment using the markdown field. "
            "Do not summarize and do not omit Original code or Proposed code."
        ),
        "markdown": markdown,
        "comment_summaries": summaries,
        "comments": agent_comments,
    }


def list_bitbucket_codeguardian_comments(workspace: str, repo_slug: str, pr_id: int, limit: int = 100) -> dict[str, Any]:
    return _run_async(list_bitbucket_codeguardian_comments_async(workspace, repo_slug, pr_id, limit))


def _extract_markdown_section(body: str, label: str) -> str:
    pattern = rf"\*\*{re.escape(label)}:\*\*\s*(.*?)(?=\n\n\*\*|\Z)"
    match = re.search(pattern, body, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_fenced_code_after_label(body: str, label: str) -> str:
    pattern = rf"\*\*{re.escape(label)}:\*\*\s*```[^\n`]*\n(.*?)\n```"
    match = re.search(pattern, body, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _comment_summary(
    comment: dict[str, Any],
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    body = str(comment.get("body") or "")
    current_code = _extract_fenced_code_after_label(body, "Block to substitute")
    proposed_code = _extract_fenced_code_after_label(body, "Proposed Code")
    problem = (
        _extract_markdown_section(body, "Problems")
        or _extract_markdown_section(body, "Performance issue")
        or _extract_markdown_section(body, "Optimization opportunity")
    )
    proposal = (
        _extract_markdown_section(body, "Solutions")
        or _extract_markdown_section(body, "Suggested performance improvement")
        or _extract_markdown_section(body, "Suggested optimization")
    )
    return {
        "index": index,
        "id": comment.get("id"),
        "file": comment.get("file"),
        "line": comment.get("line"),
        "issue_keys": comment.get("issue_keys", []),
        "is_optimization": comment.get("is_optimization"),
        "type": "Optimization" if comment.get("is_optimization") else "SonarQube",
        "problem": problem,
        "proposal": proposal,
        "original_code": current_code,
        "proposed_code": proposed_code,
        "has_code_blocks": bool(current_code and proposed_code),
        "replaceable": bool(current_code and proposed_code and normalize_code_block(current_code) != normalize_code_block(proposed_code)),
        "markdown": _format_comment_markdown(
            index=index,
            comment_id=comment.get("id"),
            workspace=workspace,
            repo_slug=repo_slug,
            pr_id=pr_id,
            file=str(comment.get("file") or ""),
            line=comment.get("line"),
            problem=problem,
            proposal=proposal,
            original_code=current_code,
            proposed_code=proposed_code,
        ),
    }


def list_codeguardian_comment_summaries(workspace: str, repo_slug: str, pr_id: int) -> dict[str, Any]:
    result = list_bitbucket_codeguardian_comments(workspace, repo_slug, pr_id, limit=100)
    summaries = result["comment_summaries"]
    markdown = "\n\n---\n\n".join(summary["markdown"] for summary in summaries)
    return {
        "workspace": workspace,
        "repo_slug": repo_slug,
        "pr_id": pr_id,
        "total": len(summaries),
        "display_instruction": (
            "Answer in English. Show every comment using the provided markdown field. "
            "Do not summarize and do not omit original_code or proposed_code."
        ),
        "markdown": markdown,
        "comments": summaries,
    }


async def _resolve_single_open_pr_target(workspace: str = "", repo_slug: str = "") -> dict[str, Any]:
    if repo_slug.strip():
        prs = await list_open_pull_requests_async(workspace, repo_slug)
        matches = [
            {
                **pr,
                "workspace": prs["workspace"],
                "repo_slug": prs["repo_slug"],
            }
            for pr in prs["pull_requests"]
        ]
    else:
        matches = []
        for repository in _detect_bitbucket_repositories(workspace):
            prs = await list_open_pull_requests_async(repository["workspace"], repository["repo_slug"])
            matches.extend([
                {
                    **pr,
                    "workspace": repository["workspace"],
                    "repo_slug": repository["repo_slug"],
                }
                for pr in prs["pull_requests"]
            ])

    if not matches:
        raise RuntimeError("No open pull requests found for the requested repository context")
    if len(matches) > 1:
        options = "; ".join(
            f"{pr['workspace']}/{pr['repo_slug']} PR {pr['id']}: {pr['title']}"
            for pr in matches
        )
        raise RuntimeError(f"Multiple open pull requests found. Choose one explicitly: {options}")
    return matches[0]


async def review_codeguardian_suggestions_async(
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int = 0,
    limit: int = 100,
    start: int = 1,
    count: int = 1,
) -> str:
    if pr_id:
        workspace = _resolve_workspace(workspace)
        repo_slug = _resolve_repo_slug(repo_slug)
        target = {"workspace": workspace, "repo_slug": repo_slug, "id": pr_id, "title": ""}
    else:
        target = await _resolve_single_open_pr_target(workspace, repo_slug)

    result = await list_bitbucket_codeguardian_comments_async(
        target["workspace"],
        target["repo_slug"],
        int(target["id"]),
        limit,
    )
    suggestions = [summary for summary in result["comment_summaries"] if summary.get("replaceable")]
    if not suggestions:
        return (
            f"No replaceable CodeGuardian suggestions found for "
            f"{target['workspace']}/{target['repo_slug']} PR {target['id']}."
        )

    safe_start = max(1, start)
    safe_count = max(1, min(count, 5))
    selected = suggestions[safe_start - 1:safe_start - 1 + safe_count]
    if not selected:
        return (
            f"No suggestion found at position {safe_start}. "
            f"There are {len(suggestions)} replaceable suggestions for "
            f"{target['workspace']}/{target['repo_slug']} PR {target['id']}."
        )

    markdown = "\n\n---\n\n".join(summary["markdown"] for summary in selected)
    returned_indices = [int(summary.get("index") or 0) for summary in selected]
    returned_comment_ids = [int(summary.get("id") or 0) for summary in selected]
    next_start = safe_start + len(selected)
    next_step = (
        f"Ask the developer whether to accept or skip suggestion {safe_start}."
        if len(selected) == 1
        else f"Ask the developer which suggestions from {safe_start} to {next_start - 1} to accept or skip."
    )
    if next_start <= len(suggestions):
        next_step += (
            f" To continue reviewing, call this tool again with start={next_start} "
            f"and count={safe_count}."
        )

    return (
        "Render the following CodeGuardian suggestion exactly as provided. "
        "Do not summarize it. Do not omit Original code. Do not omit Proposed code.\n\n"
        f"# CodeGuardian review suggestions for {target['workspace']}/{target['repo_slug']} "
        f"PR {target['id']}\n\n"
        f"Requested start={safe_start}, count={safe_count}.\n"
        f"Returned suggestion indices={returned_indices}.\n"
        f"Returned comment ids={returned_comment_ids}.\n\n"
        f"Showing suggestion {safe_start}"
        f"{f' to {next_start - 1}' if len(selected) > 1 else ''} "
        f"of {len(suggestions)}.\n\n"
        f"{markdown}\n\n"
        f"Next step: {next_step} "
        "After the developer chooses, call the CodeGuardian apply-approved-suggestions tool "
        "with the accepted numbers. Do not ask the developer to open Bitbucket."
    )


def review_codeguardian_suggestions(
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int = 0,
    limit: int = 100,
    start: int = 1,
    count: int = 1,
) -> str:
    return _run_async(review_codeguardian_suggestions_async(workspace, repo_slug, pr_id, limit, start, count))


def _parse_selection(selection: str, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = selection.strip().lower()
    if cleaned in {"all", "todos", "todas", "*"}:
        return suggestions

    numeric_tokens = [int(value) for value in re.findall(r"\d+", cleaned)]
    selected_ids = {
        int(value)
        for value in re.findall(r"(?:id|comment)\s*[:#]?\s*(\d+)", cleaned)
    }
    selected = []
    for suggestion in suggestions:
        index = int(suggestion.get("index") or 0)
        comment_id = int(suggestion.get("id") or 0)
        if index in numeric_tokens or comment_id in numeric_tokens or comment_id in selected_ids:
            selected.append(suggestion)
    return selected


def _guess_code_fence_language(file: str) -> str:
    suffix = Path(file).suffix.lower()
    mapping = {
        ".py": "python",
        ".java": "java",
        ".js": "javascript",
        ".ts": "typescript",
        ".groovy": "groovy",
        ".xml": "xml",
        ".json": "json",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".sh": "bash",
    }
    return mapping.get(suffix, "")


def _format_comment_markdown(
    index: int | None,
    comment_id: Any,
    workspace: str,
    repo_slug: str,
    pr_id: int | None,
    file: str,
    line: Any,
    problem: str,
    proposal: str,
    original_code: str,
    proposed_code: str,
) -> str:
    language = _guess_code_fence_language(file)
    title = f"## Suggestion {index}" if index is not None else f"## Comment {comment_id}"
    explanation = "\n\n".join(part for part in [problem, proposal] if part.strip())
    return (
        f"{title}\n\n"
        f"Comment ID: `{comment_id}`\n\n"
        f"File: {file}\n\n"
        f"Line: {line}\n\n"
        f"Original code:\n"
        f"```{language}\n{original_code}\n```\n\n"
        f"Explanation: {explanation}\n\n"
        f"Proposed code:\n"
        f"```{language}\n{proposed_code}\n```"
    )


def _workspace_root() -> Path:
    return Path(os.getenv("CODEGUARDIAN_WORKSPACE_ROOT", "/workspace")).resolve()


def _resolve_local_repo_path(repo_slug: str, local_repo_path: str = "") -> Path:
    root = _workspace_root()
    normalized_repo_slug = normalize_repo_slug(repo_slug)
    candidates = []
    if local_repo_path.strip():
        provided = Path(local_repo_path.strip())
        candidates.append(provided if provided.is_absolute() else root / provided)
    candidates.extend([root / normalized_repo_slug, root / f"codeguardian-{normalized_repo_slug}", root / repo_slug])

    for candidate in candidates:
        resolved = candidate.resolve()
        if root not in resolved.parents and resolved != root:
            continue
        if resolved.is_dir():
            return resolved

    raise RuntimeError(f"Local repository for {repo_slug} was not found under {root}")


async def _read_pr_comment_async(workspace: str, repo_slug: str, pr_id: int, comment_id: int) -> dict[str, Any]:
    workspace = _resolve_workspace(workspace)
    normalized_repo_slug = _resolve_repo_slug(repo_slug)
    comments = (await list_bitbucket_codeguardian_comments_async(workspace, normalized_repo_slug, pr_id))["comments"]
    data = next((comment for comment in comments if int(comment.get("id") or 0) == int(comment_id)), None)
    if data is None:
        raise RuntimeError(f"Comment {comment_id} was not found in PR {pr_id}")
    raw = str(data.get("body") or "")
    if not is_agent_comment(raw):
        raise RuntimeError("The selected comment is not a CodeGuardian comment")
    return {
        "id": data.get("id"),
        "file": data.get("file"),
        "line": data.get("line"),
        "body": raw,
        "issue_keys": extract_issue_key(raw),
    }


def _read_pr_comment(workspace: str, repo_slug: str, pr_id: int, comment_id: int) -> dict[str, Any]:
    return _run_async(_read_pr_comment_async(workspace, repo_slug, pr_id, comment_id))


async def apply_codeguardian_comment_replacement_async(
    workspace: str,
    repo_slug: str,
    pr_id: int,
    comment_id: int,
    local_repo_path: str = "",
    dry_run: bool = True,
) -> dict[str, Any]:
    workspace = _resolve_workspace(workspace)
    repo_slug = _resolve_repo_slug(repo_slug)
    comment = await _read_pr_comment_async(workspace, repo_slug, pr_id, comment_id)
    file_path = str(comment.get("file") or "").strip()
    if not file_path:
        raise RuntimeError("The selected comment is not attached to a file")

    original_code = _extract_fenced_code_after_label(comment["body"], "Block to substitute")
    proposed_code = _extract_fenced_code_after_label(comment["body"], "Proposed Code")
    if not original_code or not proposed_code:
        raise RuntimeError("The selected comment does not contain replaceable code blocks")
    if normalize_code_block(original_code) == normalize_code_block(proposed_code):
        raise RuntimeError("The proposed code is identical to the current code")

    repo_path = _resolve_local_repo_path(repo_slug, local_repo_path)
    target_path = (repo_path / file_path).resolve()
    if repo_path not in target_path.parents:
        raise RuntimeError("Resolved file path escapes the local repository")
    if not target_path.is_file():
        raise RuntimeError(f"File not found in local repository: {file_path}")

    content = target_path.read_text(encoding="utf-8")
    normalized_content = normalize_code_block(content)
    if normalize_code_block(original_code) not in normalized_content:
        raise RuntimeError("The current code block was not found in the local file")

    occurrences = content.count(original_code)
    if occurrences != 1:
        # Fall back to normalized matching only for diagnostics; writing must be exact and unique.
        raise RuntimeError(f"The current code block must appear exactly once, found {occurrences}")

    new_content = content.replace(original_code, proposed_code, 1)
    result = {
        "dry_run": dry_run,
        "comment_id": comment_id,
        "repo_path": str(repo_path),
        "file": file_path,
        "line": comment.get("line"),
        "issue_keys": comment.get("issue_keys", []),
        "changed": content != new_content,
    }

    if not dry_run:
        target_path.write_text(new_content, encoding="utf-8")
        result["applied"] = True
    else:
        result["applied"] = False

    return result


def apply_codeguardian_comment_replacement(
    workspace: str,
    repo_slug: str,
    pr_id: int,
    comment_id: int,
    local_repo_path: str = "",
    dry_run: bool = True,
) -> dict[str, Any]:
    return _run_async(apply_codeguardian_comment_replacement_async(
        workspace,
        repo_slug,
        pr_id,
        comment_id,
        local_repo_path,
        dry_run,
    ))


async def apply_approved_codeguardian_suggestions_async(
    selection: str,
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int = 0,
    local_repo_path: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    if pr_id:
        workspace = _resolve_workspace(workspace)
        repo_slug = _resolve_repo_slug(repo_slug)
        target = {"workspace": workspace, "repo_slug": repo_slug, "id": pr_id}
    else:
        target = await _resolve_single_open_pr_target(workspace, repo_slug)

    comments = await list_bitbucket_codeguardian_comments_async(
        target["workspace"],
        target["repo_slug"],
        int(target["id"]),
        limit=100,
    )
    suggestions = [
        summary
        for summary in comments["comment_summaries"]
        if summary.get("replaceable")
    ]
    selected = _parse_selection(selection, suggestions)
    if not selected:
        raise RuntimeError(
            "No suggestions matched the approved selection. Use suggestion numbers from the review output."
        )

    results = []
    for suggestion in selected:
        try:
            applied = await apply_codeguardian_comment_replacement_async(
                target["workspace"],
                target["repo_slug"],
                int(target["id"]),
                int(suggestion["id"]),
                local_repo_path,
                dry_run,
            )
            applied["suggestion_index"] = suggestion.get("index")
            results.append(applied)
        except Exception as exc:
            results.append({
                "suggestion_index": suggestion.get("index"),
                "comment_id": suggestion.get("id"),
                "file": suggestion.get("file"),
                "applied": False,
                "error": str(exc),
            })

    return {
        "workspace": target["workspace"],
        "repo_slug": target["repo_slug"],
        "pr_id": int(target["id"]),
        "dry_run": dry_run,
        "requested_selection": selection,
        "matched_suggestions": len(selected),
        "applied_count": len([result for result in results if result.get("applied")]),
        "failed_count": len([result for result in results if result.get("error")]),
        "results": results,
    }


def apply_approved_codeguardian_suggestions(
    selection: str,
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int = 0,
    local_repo_path: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    return _run_async(apply_approved_codeguardian_suggestions_async(
        selection,
        workspace,
        repo_slug,
        pr_id,
        local_repo_path,
        dry_run,
    ))


async def get_pr_review_status_async(workspace: str, repo_slug: str, pr_id: int) -> dict[str, Any]:
    workspace = _resolve_workspace(workspace)
    normalized_repo_slug = _resolve_repo_slug(repo_slug)
    pr = await _call_bitbucket_pull_request(
        "get",
        {
            "workspaceId": workspace,
            "repoId": normalized_repo_slug,
            "prId": int(pr_id),
        },
    )
    comments = await list_bitbucket_codeguardian_comments_async(workspace, normalized_repo_slug, pr_id)
    comment_summaries = comments["comment_summaries"]
    return {
        "workspace": workspace,
        "repo_slug": normalized_repo_slug,
        "pr_id": pr_id,
        "title": pr.get("title"),
        "state": pr.get("state"),
        "source_branch": ((pr.get("source") or {}).get("branch") or {}).get("name"),
        "destination_branch": ((pr.get("destination") or {}).get("branch") or {}).get("name"),
        "codeguardian_comments": comments["total_codeguardian_comments"],
        "optimization_comments": len([c for c in comment_summaries if c["is_optimization"]]),
        "sonarqube_comments": len([c for c in comment_summaries if not c["is_optimization"]]),
        "comments_tool": "Use list_codeguardian_comments to see every comment with code blocks.",
    }


def get_pr_review_status(workspace: str, repo_slug: str, pr_id: int) -> dict[str, Any]:
    return _run_async(get_pr_review_status_async(workspace, repo_slug, pr_id))


def query_prometheus(query: str) -> dict[str, Any]:
    return _get_json(
        f"{_prometheus_base_url()}/api/v1/query",
        params={"query": query},
    )


def get_codeguardian_metrics(repository: str = "") -> dict[str, Any]:
    metrics = [
        "codeguardian_sonar_findings_total",
        "codeguardian_generated_issues_total",
        "codeguardian_final_issues_total",
        "codeguardian_comments_created_total",
        "codeguardian_comments_reused_total",
        "codeguardian_comments_deleted_total",
        "codeguardian_performance_candidates_total",
        "codeguardian_performance_suggestions_total",
        "codeguardian_analysis_total_tokens",
        "codeguardian_last_execution_timestamp",
    ]

    values: dict[str, Any] = {}
    selector = f'{{repository="{repository}"}}' if repository else ""
    for metric in metrics:
        result = query_prometheus(f"{metric}{selector}")
        values[metric] = ((result.get("data") or {}).get("result") or [])

    return {
        "repository": repository or "all",
        "metrics": values,
    }


def get_jenkins_build_summary(job_name: str, build_number: str = "lastBuild") -> dict[str, Any]:
    safe_job_path = "/".join(f"job/{part}" for part in job_name.strip("/").split("/") if part)
    if not safe_job_path:
        raise RuntimeError("job_name is required")

    data = _get_json(
        f"{_jenkins_base_url()}/{safe_job_path}/{build_number}/api/json",
        headers=jenkins_headers(),
        params={
            "tree": (
                "id,number,result,building,duration,timestamp,url,fullDisplayName,"
                "actions[parameters[name,value]],changeSet[items[msg,author[fullName]]]"
            )
        },
    )
    return {
        "job_name": job_name,
        "build_number": data.get("number"),
        "result": data.get("result"),
        "building": data.get("building"),
        "duration_ms": data.get("duration"),
        "timestamp": data.get("timestamp"),
        "url": data.get("url"),
        "full_display_name": data.get("fullDisplayName"),
        "changes": ((data.get("changeSet") or {}).get("items") or []),
    }
