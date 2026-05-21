import asyncio
import base64
import hashlib
import json
import os
import urllib.error
import urllib.request

from mcp import ClientSession

from codeguardian.atlassian import atlassian_rovo_session
from codeguardian.comments import comment_content, extract_issue_key, is_agent_comment
from codeguardian.config import CODEGUARDIAN_AGENT_MARKER, CODEGUARDIAN_SUMMARY_TITLE
from codeguardian.logging_utils import logger
from codeguardian.models import AgentExecutionError, CommentSyncResult, Decision, Issue
from codeguardian.validation import group_key, issue_key


async def get_pull_request_comments(
    session: ClientSession,
    pr_id: str,
    repo_slug: str,
    workspace: str,
) -> list[dict]:
    try:
        all_comments = []
        page = 1

        while True:
            results = await session.call_tool(
                name="bitbucketPullRequest",
                arguments={
                    "action": "comments",
                    "workspaceId": workspace,
                    "repoId": repo_slug,
                    "prId": int(pr_id),
                    "pagelen": 100,
                    "page": page,
                    "sort": "-created_on",
                },
            )

            comments_data = json.loads(results.content[0].text)

            if isinstance(comments_data, dict):
                if isinstance(comments_data.get("values"), list):
                    page_comments = comments_data.get("values", [])
                elif isinstance(comments_data.get("comments"), list):
                    page_comments = comments_data.get("comments", [])
                else:
                    page_comments = []
            elif isinstance(comments_data, list):
                page_comments = comments_data
            else:
                page_comments = []

            if not page_comments:
                break

            all_comments.extend(page_comments)

            if len(page_comments) < 100:
                break

            page += 1

        return all_comments

    except Exception as e:
        logger.error(f"Failed to retrieve pull request comments: {e}")
        raise


def get_agent_summary_comment_ids(comments: list[dict]) -> set[int]:
    summary_comment_ids: set[int] = set()

    for comment in comments:
        if comment.get("deleted", False):
            continue

        if comment.get("parent"):
            continue

        raw_text = (comment.get("content", {}) or {}).get("raw", "") or comment.get("content", "")
        normalized_text = raw_text.replace(CODEGUARDIAN_AGENT_MARKER, "", 1).strip()

        if (is_agent_comment(raw_text) or normalized_text.startswith(CODEGUARDIAN_SUMMARY_TITLE) or
                raw_text.strip().startswith(CODEGUARDIAN_SUMMARY_TITLE)):
            if normalized_text.startswith(CODEGUARDIAN_SUMMARY_TITLE) or raw_text.strip().startswith(
                    CODEGUARDIAN_SUMMARY_TITLE):
                comment_id = comment.get("id")
                if comment_id is not None:
                    summary_comment_ids.add(int(comment_id))

    return summary_comment_ids


def is_comment_resolved(comment: dict) -> bool:
    state = str(comment.get("state") or comment.get("status") or "").strip().lower()
    if state == "resolved":
        return True
    if comment.get("resolved") is True:
        return True
    resolution = comment.get("resolution")
    return bool(resolution and resolution is not False)


async def get_inline_comments(session: ClientSession, pr_id: str, repo_slug: str, workspace: str) -> dict[int, dict]:
    try:
        comments = await get_pull_request_comments(session, pr_id, repo_slug, workspace)

        active_inline_comments = {}

        for comment in comments:
            if comment.get("deleted", False):
                continue

            if is_comment_resolved(comment):
                continue

            if comment.get("parent"):
                continue

            if not comment.get("inline"):
                continue

            raw_text = (comment.get("content", {}) or {}).get("raw", "") or comment.get("content", "")
            comment_id = int(comment.get("id"))

            if not is_agent_comment(raw_text):
                continue

            issue_keys = extract_issue_key(raw_text)

            inline_data = comment.get("inline") or {}
            file_path = (inline_data.get("path") or "").strip()
            line_to = int(inline_data.get("to") or inline_data.get("from") or 0)

            active_inline_comments[comment_id] = {
                "issue_keys": set(issue_keys),
                "file_path": file_path,
                "line_to": line_to,
                "content_hash": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
            }

        return active_inline_comments

    except Exception as e:
        logger.error(f"Failed to retrieve inline comments: {e}")
        raise


def get_bitbucket_basic_auth_headers() -> dict[str, str] | None:
    bitbucket_username = (os.getenv("BITBUCKET_EMAIL") or os.getenv("BITBUCKET_USERNAME") or "").strip()
    bitbucket_password = (os.getenv("BITBUCKET_API_TOKEN") or os.getenv("BITBUCKET_APP_TOKEN") or "").strip()

    if not bitbucket_username or not bitbucket_password:
        return None

    auth_raw = f"{bitbucket_username}:{bitbucket_password}".encode("utf-8")
    auth_b64 = base64.b64encode(auth_raw).decode("ascii")

    return {
        "Accept": "application/json",
        "Authorization": f"Basic {auth_b64}",
    }


def ensure_bitbucket_rest_auth() -> None:
    if not get_bitbucket_basic_auth_headers():
        raise AgentExecutionError("Missing BITBUCKET_EMAIL/BITBUCKET_API_TOKEN for Bitbucket REST inline comments")


def get_bitbucket_api_base_url() -> str:
    return os.getenv("BITBUCKET_URL", "https://api.bitbucket.org/2.0").rstrip("/")


def build_pullrequest_comment_url(
    pr_id: str,
    repo_slug: str,
    comment_id: str,
    workspace: str,
) -> str:
    base_url = get_bitbucket_api_base_url()
    return (f"{base_url}/repositories/{workspace}/{repo_slug}"
            f"/pullrequests/{pr_id}/comments/{comment_id}")


def comment_sync_mode() -> str:
    mode = (os.getenv("CODEGUARDIAN_COMMENT_SYNC_MODE") or "resolve").strip().lower()
    if mode not in {"resolve", "delete", "keep"}:
        logger.warning("Unsupported CODEGUARDIAN_COMMENT_SYNC_MODE=%s. Falling back to resolve.", mode)
        return "resolve"
    return mode


async def create_inline_comment_by_rest(
    pr_id: str,
    repo_slug: str,
    workspace: str,
    file_path: str,
    line_to: int,
    content: str,
) -> bool:
    headers = get_bitbucket_basic_auth_headers()
    if not headers:
        return False

    create_url = f"{get_bitbucket_api_base_url()}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/comments"

    payload = json.dumps({
        "content": {
            "raw": content,
        },
        "inline": {
            "path": file_path,
            "to": line_to,
        },
    }).encode("utf-8")

    def _create_comment() -> int:
        req = urllib.request.Request(
            url=create_url,
            data=payload,
            headers={
                **headers, "Content-Type": "application/json"
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return int(resp.status)

    try:
        status = await asyncio.to_thread(_create_comment)
        return status in (200, 201)
    except urllib.error.HTTPError as e:
        if e.code in (200, 201):
            return True
        logger.error(
            "Failed to create inline comment by REST: status=%s repo=%s pr=%s file=%s line=%s",
            e.code,
            repo_slug,
            pr_id,
            file_path,
            line_to,
        )
        return False
    except Exception as e:
        logger.error(
            "Unexpected error creating inline comment by REST: repo=%s pr=%s file=%s line=%s error=%s",
            repo_slug,
            pr_id,
            file_path,
            line_to,
            e,
        )
        return False


async def delete_inline_comment_by_rest(
    pr_id: str,
    repo_slug: str,
    comment_id: str,
    workspace: str,
) -> bool:
    headers = get_bitbucket_basic_auth_headers()
    if not headers:
        return False

    delete_url = build_pullrequest_comment_url(pr_id, repo_slug, comment_id, workspace)

    def _delete_comment() -> int:
        req = urllib.request.Request(
            url=delete_url,
            headers=headers,
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return int(resp.status)

    try:
        status = await asyncio.to_thread(_delete_comment)
        return status in (200, 204)
    except urllib.error.HTTPError as e:
        if e.code in (200, 204):
            return True
        logger.error(
            "Failed to delete inline comment by REST: status=%s repo=%s pr=%s comment_id=%s",
            e.code,
            repo_slug,
            pr_id,
            comment_id,
        )
        return False
    except Exception as e:
        logger.error(
            "Unexpected error deleting inline comment by REST: repo=%s pr=%s comment_id=%s error=%s",
            repo_slug,
            pr_id,
            comment_id,
            e,
        )
        return False


async def resolve_inline_comment_by_rest(
    pr_id: str,
    repo_slug: str,
    comment_id: str,
    workspace: str,
) -> bool:
    headers = get_bitbucket_basic_auth_headers()
    if not headers:
        return False

    resolve_url = f"{build_pullrequest_comment_url(pr_id, repo_slug, comment_id, workspace)}/resolve"

    def _resolve_comment() -> int:
        req = urllib.request.Request(
            url=resolve_url,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return int(resp.status)

    try:
        status = await asyncio.to_thread(_resolve_comment)
        return status == 200
    except urllib.error.HTTPError as e:
        if e.code == 200:
            return True
        if e.code == 404:
            logger.info("CodeGuardian comment already unavailable while resolving: %s", comment_id)
            return True
        if e.code == 409:
            logger.info("CodeGuardian comment resolve conflict, treating as non-fatal: %s", comment_id)
            return True
        logger.error(
            "Failed to resolve inline comment by REST: status=%s repo=%s pr=%s comment_id=%s",
            e.code,
            repo_slug,
            pr_id,
            comment_id,
        )
        return False
    except Exception as e:
        logger.error(
            "Unexpected error resolving inline comment by REST: repo=%s pr=%s comment_id=%s error=%s",
            repo_slug,
            pr_id,
            comment_id,
            e,
        )
        return False


async def delete_comment_ids(
    pr_id: str,
    repo_slug: str,
    workspace: str,
    comment_ids: set[int],
) -> tuple[set[int], set[int]]:
    deleted_comment_ids: set[int] = set()
    failed_comment_ids: set[int] = set()

    for comment_id in sorted(comment_ids):
        deleted = await delete_inline_comment_by_rest(
            pr_id,
            repo_slug,
            str(comment_id),
            workspace,
        )
        if deleted:
            deleted_comment_ids.add(comment_id)
            logger.info("Inline comment removed: %s", comment_id)
            await asyncio.sleep(0.2)
        else:
            failed_comment_ids.add(comment_id)
            logger.info("Comment %s could not be deleted", comment_id)

    return deleted_comment_ids, failed_comment_ids


async def resolve_comment_ids(
    pr_id: str,
    repo_slug: str,
    workspace: str,
    comment_ids: set[int],
) -> tuple[set[int], set[int]]:
    resolved_comment_ids: set[int] = set()
    failed_comment_ids: set[int] = set()

    for comment_id in sorted(comment_ids):
        resolved = await resolve_inline_comment_by_rest(
            pr_id,
            repo_slug,
            str(comment_id),
            workspace,
        )
        if resolved:
            resolved_comment_ids.add(comment_id)
            logger.info("Inline comment resolved: %s", comment_id)
            await asyncio.sleep(0.2)
        else:
            failed_comment_ids.add(comment_id)
            logger.info("Comment %s could not be resolved", comment_id)

    return resolved_comment_ids, failed_comment_ids


async def post_issue_group_comment(
    pr_id: str,
    repo_slug: str,
    issues: list[Issue],
    workspace: str,
) -> bool:
    try:
        if not issues:
            return False

        base_issue = max(
            issues,
            key=lambda i: int(getattr(i, "original_end_line", i.line) or i.line) - int(
                getattr(i, "original_start_line", i.line) or i.line),
        )

        if not os.path.exists(base_issue.file):
            logger.info("Skipping inline comment for missing file: %s", base_issue.file)
            return False

        line_end = max(int(getattr(i, "original_end_line", i.line) or i.line) for i in issues)
        content = comment_content(issues)

        created = await create_inline_comment_by_rest(
            pr_id=pr_id,
            repo_slug=repo_slug,
            workspace=workspace,
            file_path=base_issue.file,
            line_to=line_end,
            content=content,
        )

        if created:
            if len(issues) == 1:
                logger.info("Inline comment added")
            else:
                logger.info("Inline comment added with %s issues", len(issues))

        return created
    except Exception as e:
        logger.error(f"Failed to add inline comment: {e}")
        raise


async def delete_agent_summary_comments(
    session: ClientSession,
    pr_id: str,
    repo_slug: str,
    workspace: str,
) -> int:
    comments = await get_pull_request_comments(session, pr_id, repo_slug, workspace)
    summary_comment_ids = get_agent_summary_comment_ids(comments)

    if not summary_comment_ids:
        return 0

    deleted_comment_ids, _failed_comment_ids = await delete_comment_ids(
        pr_id,
        repo_slug,
        workspace,
        summary_comment_ids,
    )

    if deleted_comment_ids:
        logger.info(
            "Deleted %s legacy summary comments from PR %s",
            len(deleted_comment_ids),
            pr_id,
        )

    return len(deleted_comment_ids)


async def synchronize_inline_comments(
    session: ClientSession,
    pr_id: str,
    repo_slug: str,
    workspace: str,
    issues: list[Issue],
) -> CommentSyncResult:
    active_inline_comments = await get_inline_comments(session, pr_id, repo_slug, workspace)

    valid_issues = [issue for issue in issues if issue.file and issue.solution and issue.problem]

    valid_issues.sort(key=lambda i: (
        i.file,
        int(getattr(i, "original_start_line", i.line) or i.line),
        int(getattr(i, "original_end_line", i.line) or i.line),
    ))

    merge_gap = int(os.getenv("CODEGUARDIAN_GROUP_LINE_GAP", "8"))

    grouped_issues: list[list[Issue]] = []
    current_group: list[Issue] = []

    for issue in valid_issues:
        issue_start = int(getattr(issue, "original_start_line", issue.line) or issue.line)

        if not current_group:
            current_group = [issue]
            continue

        last_issue = current_group[-1]
        last_end = max(int(getattr(i, "original_end_line", i.line) or i.line) for i in current_group)

        same_group = (group_key(issue) == group_key(last_issue) and issue_start <= last_end + merge_gap)

        if same_group:
            current_group.append(issue)
        else:
            grouped_issues.append(current_group)
            current_group = [issue]

    if current_group:
        grouped_issues.append(current_group)

    desired_comments = []
    seen_desired_signatures = set()

    for issue_group in reversed(grouped_issues):
        if not issue_group:
            continue

        base_issue = max(
            issue_group,
            key=lambda i: int(getattr(i, "original_end_line", i.line) or i.line) - int(
                getattr(i, "original_start_line", i.line) or i.line),
        )

        if not os.path.exists(base_issue.file):
            continue

        line_end = max(int(getattr(i, "original_end_line", i.line) or i.line) for i in issue_group)

        issue_keys = tuple(sorted(issue_key(i) for i in issue_group))
        content = comment_content(issue_group)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        signature = (
            base_issue.file,
            line_end,
            issue_keys,
            content_hash,
        )

        if signature in seen_desired_signatures:
            continue

        seen_desired_signatures.add(signature)
        desired_comments.append({
            "signature": signature,
            "issues": issue_group,
        })

    existing: dict[tuple, int] = {}
    obsolete_comments: set[int] = set()

    for comment_id, comment_data in active_inline_comments.items():
        file_path = (comment_data.get("file_path") or "").strip()
        line_to = int(comment_data.get("line_to") or 0)
        issue_keys = tuple(sorted(comment_data.get("issue_keys") or []))
        content_hash = comment_data.get("content_hash", "")

        if not file_path or not line_to or not issue_keys or not content_hash:
            obsolete_comments.add(comment_id)
            continue

        signature = (
            file_path,
            line_to,
            issue_keys,
            content_hash,
        )

        if signature in existing:
            obsolete_comments.add(comment_id)
            continue

        existing[signature] = comment_id

    desired_signatures = {item["signature"] for item in desired_comments}

    for signature, comment_id in existing.items():
        if signature not in desired_signatures:
            obsolete_comments.add(comment_id)

    mode = comment_sync_mode()
    deleted_comment_ids: set[int] = set()
    resolved_comment_ids: set[int] = set()
    failed_comment_ids: set[int] = set()
    kept_comments = 0

    if obsolete_comments:
        if mode == "delete":
            deleted_comment_ids, failed_comment_ids = await delete_comment_ids(
                pr_id, repo_slug, workspace, obsolete_comments)
        elif mode == "keep":
            kept_comments = len(obsolete_comments)
            logger.info("Keeping %s obsolete CodeGuardian inline comments", kept_comments)
        else:
            resolved_comment_ids, failed_comment_ids = await resolve_comment_ids(
                pr_id, repo_slug, workspace, obsolete_comments)

    created_comments = 0

    for desired in desired_comments:
        if desired["signature"] in existing:
            continue

        created = await post_issue_group_comment(pr_id, repo_slug, desired["issues"], workspace)
        if created:
            created_comments += 1
        await asyncio.sleep(0.2)

    reused_comments = len(desired_comments) - created_comments
    deleted_comments = len(deleted_comment_ids)
    resolved_comments = len(resolved_comment_ids)
    failed_comments = len(failed_comment_ids)

    logger.info(
        "Inline synchronization summary: mode=%s desired=%s created=%s reused=%s resolved=%s deleted=%s kept=%s failed=%s",
        mode,
        len(desired_comments),
        created_comments,
        reused_comments,
        resolved_comments,
        deleted_comments,
        kept_comments,
        failed_comments,
    )

    return CommentSyncResult(
        desired=len(desired_comments),
        created=created_comments,
        reused=reused_comments,
        deleted=deleted_comments,
        resolved=resolved_comments,
        kept=kept_comments,
        failed=failed_comments,
        mode=mode,
    )


async def report_to_bitbucket(
    pr_id: str,
    repo_slug: str,
    workspace: str,
    decision: Decision,
) -> CommentSyncResult:
    if not pr_id or str(pr_id).lower() == "null":
        logger.error("No valid pull request ID was provided.")
        raise AgentExecutionError("Missing pull request ID")

    ensure_bitbucket_rest_auth()

    try:
        async with atlassian_rovo_session() as session_bb:
            await delete_agent_summary_comments(
                session_bb,
                pr_id,
                repo_slug,
                workspace,
            )

            sync_result = await synchronize_inline_comments(
                session_bb,
                pr_id,
                repo_slug,
                workspace,
                decision.issues,
            )

            logger.info("Comments synchronized")
            return sync_result

    except Exception as e:
        logger.error(f"Failed to report analysis results to Bitbucket: {e}")
        raise AgentExecutionError("Bitbucket reporting failed") from e
