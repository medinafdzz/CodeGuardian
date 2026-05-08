import ast
import os
import re
import subprocess
from pathlib import Path

from codeguardian.logging_utils import logger
from codeguardian.models import AgentExecutionError, BuildValidationResult, Issue
from codeguardian.text import clean_replacement_text, detect_language, normalize_code_block, read_file_lines


def issue_key(issue: Issue) -> str:
    if issue.sonar_key and issue.sonar_key != "NO_KEY":
        return issue.sonar_key
    return f"{issue.file}:{issue.line}:{issue.target_name}:{issue.severity}"


def normalize_issues(issues: list[Issue]) -> tuple[list[Issue], int]:
    prepared_issues = []
    dropped_invalid_issues = 0
    seen_sonar_keys = set()

    for issue in issues:
        issue.file = (issue.file or "").strip()
        issue.target_type = (issue.target_type or "").strip()
        issue.target_name = (issue.target_name or "").strip()
        issue.problem = re.sub(r"\s*-\s+", "\n- ", (issue.problem or "").strip()).lstrip("\n")
        issue.severity = (issue.severity or "").strip().upper()
        issue.solution = re.sub(r"\s*-\s+", "\n- ", (issue.solution or "").strip()).lstrip("\n")
        issue.original_code = clean_replacement_text(issue.original_code or "")
        issue.proposed_code = clean_replacement_text(issue.proposed_code or "")

        normalized_original_code = normalize_code_block(issue.original_code)
        normalized_proposed_code = normalize_code_block(issue.proposed_code)

        if (not normalized_original_code or not normalized_proposed_code or
                normalized_original_code == normalized_proposed_code):
            dropped_invalid_issues += 1
            continue

        if issue.original_start_line is None:
            issue.original_start_line = issue.line

        if issue.original_end_line is None:
            issue.original_end_line = issue.line

        if issue.line < 1:
            issue.line = 1

        if issue.original_start_line is not None and issue.original_start_line < 1:
            issue.original_start_line = 1

        if issue.original_end_line is not None and issue.original_end_line < 1:
            issue.original_end_line = 1

        if issue.original_start_line and issue.original_end_line:
            if issue.original_end_line < issue.original_start_line:
                issue.original_start_line, issue.original_end_line = issue.original_end_line, issue.original_start_line

        if not issue.file or not issue.problem or not issue.solution or not issue.severity:
            dropped_invalid_issues += 1
            continue

        sonar_issue_key = issue_key(issue)

        if issue.sonar_key and issue.sonar_key != "NO_KEY":
            if sonar_issue_key in seen_sonar_keys:
                continue
            seen_sonar_keys.add(sonar_issue_key)

        prepared_issues.append(issue)

    return prepared_issues, dropped_invalid_issues


def group_key(issue: Issue) -> tuple[str, str, str]:
    return (
        issue.file,
        normalize_code_block(clean_replacement_text(issue.original_code or "")),
        normalize_code_block(clean_replacement_text(issue.proposed_code or "")),
    )


def patched_file_content(issue: Issue) -> str | None:
    if not issue.file or not os.path.exists(issue.file):
        return None

    try:
        lines = read_file_lines(issue.file)
    except Exception:
        return None

    start_line = int(issue.original_start_line or issue.line or 0)
    end_line = int(issue.original_end_line or issue.line or 0)

    if start_line < 1 or end_line < start_line or end_line > len(lines):
        return None

    original_slice = "".join(lines[start_line - 1:end_line])
    normalized_file_block = normalize_code_block(original_slice)
    normalized_issue_block = normalize_code_block(issue.original_code)

    if not normalized_issue_block or normalized_file_block != normalized_issue_block:
        return None

    replacement = issue.proposed_code or ""

    if original_slice.endswith("\n") and replacement and not replacement.endswith("\n"):
        replacement += "\n"

    patched_content = "".join(lines[:start_line - 1]) + replacement + "".join(lines[end_line:])
    return patched_content


def validate_issue(issue: Issue) -> tuple[bool, str]:
    patched_content = patched_file_content(issue)
    if patched_content is None:
        return False, "original_code does not match the current file content in the expected line range"

    language = detect_language(issue.file)

    if language == "python":
        try:
            ast.parse(patched_content)
        except SyntaxError as e:
            return False, f"python syntax validation failed: {e}"

    return True, ""


def filter_valid_issues(issues: list[Issue]) -> tuple[list[Issue], int]:
    valid_issues: list[Issue] = []
    dropped_issues = 0

    for issue in issues:
        is_valid, reason = validate_issue(issue)

        if not is_valid:
            dropped_issues += 1
            logger.info(
                "Dropped issue %s for file %s line %s after patch validation: %s",
                issue.sonar_key,
                issue.file,
                issue.line,
                reason,
            )
            continue

        valid_issues.append(issue)

    return valid_issues, dropped_issues


def validate_maven_compile(workspace: str | os.PathLike[str] = ".") -> BuildValidationResult:
    workspace_path = Path(workspace)
    pom_path = workspace_path / "pom.xml"

    if not pom_path.exists():
        logger.info("Maven compile validation skipped: pom.xml not found")
        return BuildValidationResult(executed=False, success=True, reason="pom.xml not found")

    command = ["mvn", "-B", "-q", "-ntp", "-DskipTests", "compile"]
    logger.info("Running Maven compile validation")

    result = subprocess.run(
        command,
        cwd=workspace_path,
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        output = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
        if output:
            logger.error("Maven compile validation failed:\n%s", output[-4000:])
        raise AgentExecutionError("Maven compile validation failed")

    logger.info("Maven compile validation completed successfully")
    return BuildValidationResult(executed=True, success=True)
