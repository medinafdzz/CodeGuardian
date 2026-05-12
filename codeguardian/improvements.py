import ast
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import time

import google.genai as genai
from google.genai import types

from codeguardian.logging_utils import logger
from codeguardian.models import (
    AnalysisMetrics,
    Decision,
    ImprovementCandidate,
    Issue,
    IssueBatchDecision,
)
from codeguardian.text import clean_replacement_text, normalize_code_block, read_file_lines


DEFAULT_IMPROVEMENT_EXCLUSIONS = (
    ".git/",
    "node_modules/",
    "target/",
    "build/",
    ".venv/",
    "venv/",
    ".codeguardian-venv/",
    "__pycache__/",
    ".ruff_cache/",
)

IMPROVEMENT_REVIEW_RULES = """
You are CodeGuardian Improvement Review.

Goal:
- Suggest maintainability improvements for changed code only.
- Do not report security bugs, vulnerabilities, or SonarQube-style defects here.
- Focus on technical debt, readability, decomposition, testability, naming, duplication, and legacy patterns.

Strict rules:
- Return at most the requested number of issues.
- Only comment on code present in the provided diff.
- Every suggestion must be non-blocking and useful in a code review.
- Do not suggest large rewrites, architecture migrations, or project-wide refactors.
- Do not invent files, line numbers, imports, APIs, or hidden dependencies.
- original_code must be copied exactly from the current file content shown in the diff context.
- proposed_code must be a concrete replacement for original_code.
- If there is no high-confidence improvement, return an empty issues list.
- Use severity "IMPROVEMENT".
- Use sonar_key values starting with "IMPROVEMENT:".
"""


def improvements_enabled() -> bool:
    return os.getenv("CODEGUARDIAN_ENABLE_IMPROVEMENTS", "false").strip().lower() in {"1", "true", "yes", "on"}


def run_git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def improvement_exclusions() -> tuple[str, ...]:
    configured = os.getenv("CODEGUARDIAN_IMPROVEMENT_EXCLUDE", "")
    extra_patterns = [
        pattern.strip().replace("\\", "/")
        for chunk in configured.replace(";", ",").split(",")
        for pattern in chunk.splitlines()
        if pattern.strip()
    ]
    return (*DEFAULT_IMPROVEMENT_EXCLUSIONS, *extra_patterns)


def is_improvement_path_excluded(path: str, exclusions: tuple[str, ...] | None = None) -> bool:
    normalized_path = path.replace("\\", "/").lstrip("./")
    patterns = exclusions or improvement_exclusions()

    for pattern in patterns:
        normalized_pattern = pattern.replace("\\", "/").lstrip("./")
        if normalized_pattern.endswith("/") and normalized_path.startswith(normalized_pattern):
            return True
        if fnmatch.fnmatch(normalized_path, normalized_pattern):
            return True

    return False


def diff_base_ref() -> str:
    target = os.getenv("CHANGE_TARGET", "main").strip() or "main"
    candidates = [
        f"origin/{target}...HEAD",
        f"{target}...HEAD",
        "HEAD~1...HEAD",
    ]

    for candidate in candidates:
        if run_git(["diff", "--name-only", candidate]):
            return candidate

    return ""


def changed_files(base_ref: str, max_files: int) -> list[str]:
    if not base_ref:
        return []

    output = run_git(["diff", "--name-only", "--diff-filter=AM", base_ref])
    files = []
    exclusions = improvement_exclusions()

    for raw_path in output.splitlines():
        path = raw_path.strip()
        if not path or is_improvement_path_excluded(path, exclusions):
            continue
        if not os.path.isfile(path):
            continue
        files.append(path)
        if len(files) >= max_files:
            break

    return files


def build_diff_payload(base_ref: str, files: list[str], max_chars: int) -> str:
    payload_parts = []
    remaining_chars = max_chars

    for file_path in files:
        if remaining_chars <= 0:
            break

        diff = run_git(["diff", "--unified=25", base_ref, "--", file_path])
        if not diff:
            continue

        snippet = diff[:remaining_chars]
        payload_parts.append(f"### FILE: {file_path}\n```diff\n{snippet}\n```")
        remaining_chars -= len(snippet)

    return "\n\n".join(payload_parts)


def _python_node_source(lines: list[str], node: ast.AST) -> str:
    start_line = getattr(node, "lineno", 1)
    end_line = getattr(node, "end_lineno", start_line)
    return "".join(lines[start_line - 1:end_line]).rstrip()


def detect_python_improvement_candidates(
    file_path: str,
    max_function_lines: int = 60,
) -> list[ImprovementCandidate]:
    try:
        lines = read_file_lines(file_path)
        tree = ast.parse("".join(lines), filename=file_path)
    except Exception:
        return []

    candidates: list[ImprovementCandidate] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_lines = int(getattr(node, "end_lineno", node.lineno)) - node.lineno + 1
            if function_lines > max_function_lines:
                candidates.append(ImprovementCandidate(
                    file=file_path,
                    line=node.lineno,
                    language="python",
                    category="complexity",
                    reason="Function is long enough to make maintenance and testing harder.",
                    evidence=f"function_lines={function_lines};threshold={max_function_lines}",
                    original_code=_python_node_source(lines, node),
                    confidence=0.7,
                ))

        if isinstance(node, ast.ExceptHandler):
            exception_name = "bare"
            if isinstance(node.type, ast.Name):
                exception_name = node.type.id

            if node.type is None or exception_name in {"Exception", "BaseException"}:
                candidates.append(ImprovementCandidate(
                    file=file_path,
                    line=node.lineno,
                    language="python",
                    category="error_handling",
                    reason="Broad exception handling can hide unrelated failures.",
                    evidence=f"broad_exception={exception_name}",
                    original_code=_python_node_source(lines, node),
                    confidence=0.75,
                ))

    return candidates


def _line_block(lines: list[str], line_index: int, context: int = 1) -> str:
    start = max(0, line_index - context)
    end = min(len(lines), line_index + context + 1)
    return "".join(lines[start:end]).rstrip()


def detect_shell_improvement_candidates(file_path: str) -> list[ImprovementCandidate]:
    try:
        lines = read_file_lines(file_path)
    except Exception:
        return []

    candidates: list[ImprovementCandidate] = []
    unquoted_test_pattern = re.compile(r"\[\s+-[a-zA-Z]\s+\$([A-Za-z_][A-Za-z0-9_]*)\s+\]")

    for index, line in enumerate(lines):
        stripped = line.strip()

        if re.search(r"\bfor\s+\w+\s+in\s+\$\(", stripped):
            candidates.append(ImprovementCandidate(
                file=file_path,
                line=index + 1,
                language="shell",
                category="maintainability",
                reason="Command substitution in shell loops is fragile when values contain spaces or newlines.",
                evidence="command_substitution_iteration",
                original_code=_line_block(lines, index),
                confidence=0.75,
            ))

        match = unquoted_test_pattern.search(stripped)
        if match:
            variable_name = match.group(1)
            candidates.append(ImprovementCandidate(
                file=file_path,
                line=index + 1,
                language="shell",
                category="resource_handling",
                reason="Unquoted variables in shell tests can break when paths contain spaces or are empty.",
                evidence=f"unquoted_test_variable={variable_name}",
                original_code=_line_block(lines, index),
                confidence=0.8,
            ))

    return candidates


def detect_java_improvement_candidates(file_path: str) -> list[ImprovementCandidate]:
    try:
        lines = read_file_lines(file_path)
    except Exception:
        return []

    candidates: list[ImprovementCandidate] = []
    broad_catch_pattern = re.compile(r"\bcatch\s*\(\s*(Exception|Throwable)\b")
    console_print_pattern = re.compile(r"\b(System\.(?:out|err)\.println)\s*\(")

    for index, line in enumerate(lines):
        stripped = line.strip()

        catch_match = broad_catch_pattern.search(stripped)
        if catch_match:
            exception_name = catch_match.group(1)
            candidates.append(ImprovementCandidate(
                file=file_path,
                line=index + 1,
                language="java",
                category="error_handling",
                reason="Broad exception handling can hide unrelated failures.",
                evidence=f"broad_exception={exception_name}",
                original_code=_line_block(lines, index),
                confidence=0.75,
            ))

        print_match = console_print_pattern.search(stripped)
        if print_match:
            print_call = print_match.group(1)
            candidates.append(ImprovementCandidate(
                file=file_path,
                line=index + 1,
                language="java",
                category="observability",
                reason="Console prints in application code are harder to control than a project logger.",
                evidence=f"console_print={print_call}",
                original_code=_line_block(lines, index),
                confidence=0.65,
            ))

    return candidates


def detect_cpp_improvement_candidates(file_path: str) -> list[ImprovementCandidate]:
    try:
        lines = read_file_lines(file_path)
    except Exception:
        return []

    candidates: list[ImprovementCandidate] = []
    manual_new_pattern = re.compile(r"(?:^|[=\s(])new\s+[A-Za-z_][A-Za-z0-9_:<>]*\s*(?:\(|\[)")

    for index, line in enumerate(lines):
        stripped = line.strip()

        if stripped == "using namespace std;":
            candidates.append(ImprovementCandidate(
                file=file_path,
                line=index + 1,
                language="cpp",
                category="maintainability",
                reason="Using the whole std namespace can create naming conflicts in larger codebases.",
                evidence="using_namespace_std",
                original_code=_line_block(lines, index),
                confidence=0.7,
            ))

        if manual_new_pattern.search(stripped):
            candidates.append(ImprovementCandidate(
                file=file_path,
                line=index + 1,
                language="cpp",
                category="resource_management",
                reason="Manual allocation with new is easier to leak than scoped ownership.",
                evidence="manual_new_allocation",
                original_code=_line_block(lines, index),
                confidence=0.65,
            ))

    return candidates


def detect_improvement_candidates(
    files: list[str],
    max_candidates: int = 10,
) -> list[ImprovementCandidate]:
    candidates: list[ImprovementCandidate] = []

    for file_path in files:
        extension = os.path.splitext(file_path)[1].lower()

        if extension == ".py":
            candidates.extend(detect_python_improvement_candidates(file_path))
        elif extension in {".sh", ".ksh", ".bash"}:
            candidates.extend(detect_shell_improvement_candidates(file_path))
        elif extension == ".java":
            candidates.extend(detect_java_improvement_candidates(file_path))
        elif extension in {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}:
            candidates.extend(detect_cpp_improvement_candidates(file_path))

        if len(candidates) >= max_candidates:
            return candidates[:max_candidates]

    return candidates


def format_improvement_candidates(candidates: list[ImprovementCandidate]) -> str:
    if not candidates:
        return "No static improvement candidates were detected."

    payload = []
    for index, candidate in enumerate(candidates, start=1):
        payload.append({
            "id": index,
            "file": candidate.file,
            "line": candidate.line,
            "language": candidate.language,
            "category": candidate.category,
            "reason": candidate.reason,
            "evidence": candidate.evidence,
            "confidence": candidate.confidence,
            "original_code": candidate.original_code,
        })

    return json.dumps(payload, indent=2)


def build_improvement_prompt(
    project_key: str,
    max_improvements: int,
    files: list[str],
    diff_payload: str,
    candidates: list[ImprovementCandidate],
) -> str:
    return f"""
Project:
{project_key}

Maximum improvement suggestions:
{max_improvements}

Changed files:
{json.dumps(files)}

Detected improvement candidates:
{format_improvement_candidates(candidates)}

Only publish suggestions that are supported by these candidates.
Use the diff context to verify that the suggestion applies to changed code.

Diff context:
{diff_payload}
"""


def improvement_signature(project_key: str, diff_payload: str, max_improvements: int) -> str:
    raw = json.dumps({
        "project_key": project_key,
        "rules": IMPROVEMENT_REVIEW_RULES,
        "max_improvements": max_improvements,
        "diff_hash": hashlib.sha256(diff_payload.encode("utf-8")).hexdigest(),
    }, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def align_issue_to_current_file(issue: Issue) -> Issue:
    if not issue.file or not os.path.exists(issue.file) or not issue.original_code:
        return issue

    try:
        file_lines = read_file_lines(issue.file)
    except Exception:
        return issue

    original_code = clean_replacement_text(issue.original_code)
    original_lines = original_code.splitlines()
    if not original_lines:
        return issue

    window_size = len(original_lines)
    normalized_original = normalize_code_block(original_code)

    for start_index in range(0, len(file_lines) - window_size + 1):
        candidate = "".join(file_lines[start_index:start_index + window_size])
        if normalize_code_block(candidate) != normalized_original:
            continue

        issue.original_start_line = start_index + 1
        issue.original_end_line = start_index + window_size
        issue.line = issue.original_start_line
        return issue

    return issue


def analyze_improvements(project_key: str) -> Decision:
    if not improvements_enabled():
        return Decision(issues=[])

    max_files = int(os.getenv("CODEGUARDIAN_MAX_IMPROVEMENT_FILES", "4"))
    max_chars = int(os.getenv("CODEGUARDIAN_MAX_IMPROVEMENT_CHARS", "18000"))
    max_improvements = int(os.getenv("CODEGUARDIAN_MAX_IMPROVEMENTS", "3"))
    max_candidates = int(os.getenv("CODEGUARDIAN_MAX_IMPROVEMENT_CANDIDATES", "10"))

    base_ref = diff_base_ref()
    files = changed_files(base_ref, max_files)
    diff_payload = build_diff_payload(base_ref, files, max_chars)

    if not diff_payload:
        logger.info("Improvement review skipped: no suitable diff found")
        return Decision(issues=[])

    client = genai.Client(api_key=os.getenv("LLM_AUTH_TOKEN"))
    start_time = time.time()
    candidates = detect_improvement_candidates(files, max_candidates=max_candidates)
    logger.info("Improvement review detected %s static candidates", len(candidates))

    prompt = build_improvement_prompt(
        project_key=project_key,
        max_improvements=max_improvements,
        files=files,
        diff_payload=diff_payload,
        candidates=candidates,
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"{IMPROVEMENT_REVIEW_RULES}\n\n{prompt}",
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=IssueBatchDecision,
            temperature=0,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )

    prompt_tokens = 0
    response_tokens = 0
    total_tokens = 0

    try:
        prompt_tokens = int(response.usage_metadata.prompt_token_count)
        response_tokens = int(response.usage_metadata.candidates_token_count)
        total_tokens = int(response.usage_metadata.total_token_count)
    except Exception:
        pass

    try:
        decision = IssueBatchDecision.model_validate_json(response.text)
    except Exception as e:
        logger.error("Failed to parse improvement review response: %s", e)
        logger.error("The response from the model was: %s", response.text)
        return Decision(issues=[])

    issues = []
    seen_keys = set()
    signature = improvement_signature(project_key, diff_payload, max_improvements)

    for index, issue in enumerate(decision.issues[:max_improvements], start=1):
        issue.severity = "IMPROVEMENT"
        if not issue.sonar_key or not issue.sonar_key.startswith("IMPROVEMENT:"):
            issue.sonar_key = f"IMPROVEMENT:{signature}:{index}"
        if issue.sonar_key in seen_keys:
            continue
        seen_keys.add(issue.sonar_key)
        issues.append(align_issue_to_current_file(issue))

    logger.info("Improvement review produced %s suggestions", len(issues))

    return Decision(
        issues=issues,
        metrics=AnalysisMetrics(
            latency_seconds=time.time() - start_time,
            prompt_tokens=prompt_tokens,
            response_tokens=response_tokens,
            total_tokens=total_tokens,
            improvement_candidates=len(candidates),
        ),
    )
