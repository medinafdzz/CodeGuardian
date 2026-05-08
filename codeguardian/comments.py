import re

from codeguardian.config import CODEGUARDIAN_AGENT_MARKER
from codeguardian.models import Issue
from codeguardian.text import clean_replacement_text
from codeguardian.validation import issue_key


def extract_issue_key(comment_text: str) -> list[str]:
    keys = set()

    # 1) Search for ID format
    blocks = re.findall(r"<!--\s*CodeGuardian-IDs?:\s*([\s\S]*?)-->", comment_text, flags=re.IGNORECASE)

    # 2) Extract keys from the founded blocks of IDs
    for block in blocks:
        for key in re.findall(r"\bID\s*:\s*([^\s<,]+)", block, flags=re.IGNORECASE):
            keys.add(key.strip())

        # 3) Separte the ids by comma
        for legacy in re.split(r"[, \n\r\t]+", block):
            legacy = legacy.strip()
            if legacy and not legacy.upper().startswith("ID:"):
                keys.add(legacy)

    return list(keys)


def hidden_ids(issue_keys: list[str]) -> str:
    unique_keys = list(dict.fromkeys(k.strip() for k in issue_keys if k and k.strip()))
    if not unique_keys:
        return ""
    ids_lines = "\n".join(f"ID: {k}" for k in unique_keys)
    return f"<!-- CodeGuardian-IDs:\n{ids_lines}\n-->"


def wrap_agent_comment(body: str) -> str:
    return f"{CODEGUARDIAN_AGENT_MARKER}\n{body}"


def is_agent_comment(comment_text: str) -> bool:
    return CODEGUARDIAN_AGENT_MARKER in (comment_text or "")


def comment_content(issues: list[Issue]) -> str:
    if not issues:
        return ""

    issues = sorted(
        issues,
        key=lambda i: (
            int(getattr(i, "original_start_line", i.line) or i.line),
            int(getattr(i, "original_end_line", i.line) or i.line),
            i.severity,
            i.problem,
        ),
    )

    base_issue = max(
        issues,
        key=lambda i: int(getattr(i, "original_end_line", i.line) or i.line) - int(
            getattr(i, "original_start_line", i.line) or i.line),
    )

    min_line = min(int(getattr(i, "original_start_line", i.line) or i.line) for i in issues)
    max_line = max(int(getattr(i, "original_end_line", i.line) or i.line) for i in issues)

    file_extension = base_issue.file.split(".")[-1] if "." in base_issue.file else "txt"
    clean_orig = clean_replacement_text(base_issue.original_code)
    clean_prop = clean_replacement_text(base_issue.proposed_code)

    issue_keys = list(dict.fromkeys(issue_key(i) for i in issues))

    all_required_imports = []
    seen_required_imports = set()

    for issue in issues:
        for required_import in getattr(issue, "required_imports", []) or []:
            normalized_required_import = (required_import or "").strip()
            if not normalized_required_import:
                continue
            if normalized_required_import in seen_required_imports:
                continue
            seen_required_imports.add(normalized_required_import)
            all_required_imports.append(normalized_required_import)

    imports_block = ""
    if all_required_imports:
        imports_block = ("**Additional required imports:**\n"
                         f"```{file_extension}\n" +
                         "\n".join(required_import for required_import in all_required_imports) + "\n```\n\n")

    if len(issues) == 1:
        issue = issues[0]
        body = (f"### Code Issue\n\n"
                f"**File:** {issue.file}\n\n"
                f"**Lines:** {min_line}-{max_line}\n\n"
                f"**Severity:** {issue.severity}\n\n"
                f"**Problems:**\n\n{issue.problem}\n\n"
                f"**Solutions:**\n\n{issue.solution}\n\n"
                f"{imports_block}"
                f"**Block to substitute:**\n"
                f"```{file_extension}\n"
                f"{clean_orig}\n"
                f"```\n\n"
                f"**Proposed Code:**\n"
                f"```{file_extension}\n"
                f"{clean_prop}\n"
                f"```\n\n"
                f"{hidden_ids(issue_keys)}")
        return wrap_agent_comment(body)

    seen_problem_lines = set()
    problem_lines = []

    for issue in issues:
        problem_line = f"- Line {issue.line} ({issue.severity}): {issue.problem}"
        normalized_problem_line = problem_line.strip().lower()

        if normalized_problem_line in seen_problem_lines:
            continue

        seen_problem_lines.add(normalized_problem_line)
        problem_lines.append(problem_line)

    combined_problems = "\n".join(problem_lines)

    unique_solutions = []
    seen_solutions = set()

    for issue in issues:
        normalized_solution = (issue.solution or "").strip().lower()
        if normalized_solution in seen_solutions:
            continue
        seen_solutions.add(normalized_solution)
        unique_solutions.append(issue.solution.strip())

    if len(unique_solutions) == 1:
        solution_block = f"**Suggested solution:**\n{unique_solutions[0]}\n\n"
    else:
        solution_block = "**Suggested actions:**\n\n" + "\n".join(
            f"- {solution}" for solution in unique_solutions) + "\n\n"

    body = (f"### Code Issues\n\n"
            f"**File:** {base_issue.file}\n\n"
            f"**Lines:** {min_line}-{max_line}\n\n"
            f"**Detected problems:**\n\n"
            f"{combined_problems}\n\n"
            f"{solution_block}"
            f"{imports_block}"
            f"**Block to substitute:**\n"
            f"```{file_extension}\n"
            f"{clean_orig}\n"
            f"```\n\n"
            f"**Proposed Code:**\n"
            f"```{file_extension}\n"
            f"{clean_prop}\n"
            f"```\n\n"
            f"{hidden_ids(issue_keys)}")

    return wrap_agent_comment(body)
