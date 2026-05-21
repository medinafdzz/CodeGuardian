import hashlib
import json
import os
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codeguardian.logging_utils import logger
from codeguardian.models import Decision, Issue


def current_git_head() -> str:
    for env_name in ("CODEGUARDIAN_HEAD_COMMIT", "BITBUCKET_COMMIT", "GIT_COMMIT"):
        value = (os.getenv(env_name) or "").strip()
        if value:
            return value

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def stable_suggestion_id(issue: Issue) -> str:
    raw = "|".join([
        issue.source or "",
        issue.sonar_key or "",
        issue.file or "",
        str(issue.original_start_line or issue.line or ""),
        str(issue.original_end_line or issue.line or ""),
        issue.target_type or "",
        issue.target_name or "",
        issue.original_code or "",
        issue.proposed_code or "",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def suggestion_content_hash(issue: Issue) -> str:
    raw = json.dumps(
        {
            "problem": issue.problem,
            "solution": issue.solution,
            "original_code": issue.original_code,
            "proposed_code": issue.proposed_code,
            "required_imports": issue.required_imports,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def suggestion_to_result_item(issue: Issue) -> dict[str, Any]:
    line = int(issue.line or 0)
    return {
        "id": stable_suggestion_id(issue),
        "source": issue.source or "sonarqube",
        "sonar_key": issue.sonar_key or "",
        "file": issue.file or "",
        "line": line,
        "original_start_line": issue.original_start_line or line,
        "original_end_line": issue.original_end_line or line,
        "target_type": issue.target_type or "",
        "target_name": issue.target_name or "",
        "severity": issue.severity or "",
        "problem": issue.problem or "",
        "solution": issue.solution or "",
        "original_code": issue.original_code or "",
        "proposed_code": issue.proposed_code or "",
        "required_imports": list(issue.required_imports or []),
        "content_hash": suggestion_content_hash(issue),
        "status": "open",
    }


def build_results_export(
    decision: Decision,
    project_key: str,
    repository: str,
    workspace: str,
    pull_request: str,
    blocking_findings: bool = False,
) -> dict[str, Any]:
    suggestions = [suggestion_to_result_item(issue) for issue in decision.issues]
    by_severity = Counter(item["severity"] for item in suggestions if item.get("severity"))
    by_source = Counter(item["source"] for item in suggestions if item.get("source"))

    return {
        "schema_version": "1.0",
        "tool": "CodeGuardian",
        "run_id": os.getenv("BUILD_TAG") or os.getenv("BUILD_ID") or "",
        "project_key": project_key or "",
        "repository": repository or "",
        "workspace": workspace or "",
        "pull_request": str(pull_request or ""),
        "build_number": os.getenv("BUILD_NUMBER") or "",
        "head_commit": current_git_head(),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "summary": {
            "total_suggestions": len(suggestions),
            "by_severity": dict(by_severity),
            "by_source": dict(by_source),
            "blocking_findings": bool(blocking_findings),
        },
        "suggestions": suggestions,
    }


def write_results_export(path: str | os.PathLike[str], data: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def export_results_if_enabled(
    decision: Decision,
    project_key: str,
    repository: str,
    workspace: str,
    pull_request: str,
    blocking_findings: bool = False,
) -> None:
    path = (os.getenv("CODEGUARDIAN_RESULTS_PATH") or "").strip()
    if not path:
        return

    try:
        data = build_results_export(
            decision=decision,
            project_key=project_key,
            repository=repository,
            workspace=workspace,
            pull_request=pull_request,
            blocking_findings=blocking_findings,
        )
        write_results_export(path, data)
        logger.info("Exported CodeGuardian results to %s", path)
    except Exception as exc:
        logger.error("Failed to export CodeGuardian results to %s: %s", path, exc)
