# Standard imports, third-party dependencies and logging configuration
import argparse
import json
import asyncio
import os
import sys
from functools import lru_cache
import google.genai as genai
import logging
import time
import re
import base64
import urllib.request
import urllib.error
import httpx
from dataclasses import dataclass
from mcp.types import CallToolResult  # Library to manage the results of the MCP tools
from contextlib import asynccontextmanager
from mcp.client.streamable_http import streamable_http_client
from google.genai import (
    types,)  # Library to manage the configuration and types for the Gemini model API
from mcp import (
    ClientSession,
    StdioServerParameters,
)  # Library to manage the client session with the MCP tools
from mcp.client.stdio import (
    stdio_client,)  # Library to interact with the MCP tools using standard input/output
from pydantic import BaseModel, Field  # Library for json formating and validation
from prometheus_client import (
    CollectorRegistry,
    Gauge,
    push_to_gateway,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger("codeguardian")
logger.setLevel(logging.INFO)

logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)


# Core models, custom exceptions and global constants used by the agent
class Issue(BaseModel):
    sonar_key: str
    file: str
    target_type: str
    target_name: str
    line: int
    original_start_line: int | None = None
    original_end_line: int | None = None
    problem: str
    severity: str
    solution: str
    original_code: str
    proposed_code: str
    required_imports: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    decline_pr: bool
    issues: list[Issue]
    comment: str


@dataclass(frozen=True)
class ScopeInfo:
    kind: str
    name: str
    start_line: int
    end_line: int


class IssueBatchDecision(BaseModel):
    issues: list[Issue]


class AgentExecutionError(Exception):
    """Raised when the agent cannot complete a required execution step."""


CODEGUARDIAN_SUMMARY_TITLE = "**CodeGuardian Analysis Summary**"
CODEGUARDIAN_AGENT_MARKER = "<!-- CodeGuardian-Agent -->"
ATLASSIAN_ROVO_MCP_URL = "https://mcp.atlassian.com/v1/mcp"


# Atlassian Rovo MCP connection helpers using an authenticated HTTP client
def get_atlassian_mcp_url() -> str:
    return (os.getenv("ATLASSIAN_MCP_URL") or ATLASSIAN_ROVO_MCP_URL).strip()


def get_atlassian_mcp_auth() -> httpx.Auth:
    auth_header = (os.getenv("ATLASSIAN_MCP_AUTH_HEADER") or "").strip()

    if not auth_header:
        raise AgentExecutionError("Missing ATLASSIAN_MCP_AUTH_HEADER for Atlassian Rovo MCP")

    if auth_header.startswith("Basic "):
        token = auth_header[len("Basic "):].strip()
        try:
            decoded = base64.b64decode(token).decode("utf-8")
            username, password = decoded.split(":", 1)
        except Exception as e:
            raise AgentExecutionError(
                "Invalid Basic auth format in ATLASSIAN_MCP_AUTH_HEADER"
            ) from e

        return httpx.BasicAuth(username, password)

    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):].strip()

        class BearerAuth(httpx.Auth):
            def auth_flow(self, request):
                request.headers["Authorization"] = f"Bearer {token}"
                yield request

        return BearerAuth()

    raise AgentExecutionError("Unsupported ATLASSIAN_MCP_AUTH_HEADER scheme")

@asynccontextmanager
async def atlassian_rovo_session():
    async with httpx.AsyncClient(
        auth=get_atlassian_mcp_auth(),
        follow_redirects=True,
    ) as custom_client:
        async with streamable_http_client(
            get_atlassian_mcp_url(),
            http_client=custom_client,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


# Generic file reading and text normalization helpers
@lru_cache(maxsize=256)
def read_file_lines(filepath: str) -> list[str]:
    if not os.path.exists(filepath):
        raise FileNotFoundError("File not found.")
    with open(filepath, "r", encoding="utf-8") as file:
        return file.readlines()


def clean_replacement_text(value: str) -> str:
    return value.replace('\\n', '\n').strip('`').strip()


def normalize_code_block(text: str) -> str:
    if not text:
        return ""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().splitlines()).strip()


# Source code structure helpers to detect language and affected scope
def detect_language(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    mapping = {
        ".py": "python",
        ".java": "java",
        ".js": "javascript",
        ".ts": "typescript",
        ".go": "go",
        ".cs": "csharp",
        ".cpp": "cpp",
        ".cxx": "cpp",
        ".cc": "cpp",
        ".c": "c",
        ".php": "php",
        ".rb": "ruby",
        ".rs": "rust",
        ".kt": "kotlin",
        ".swift": "swift",
    }
    return mapping.get(ext, "unknown")


def resolve_scope_with_parser(filepath: str, line_number: int, language: str) -> ScopeInfo:

    lines = read_file_lines(filepath)

    if not lines:
        return ScopeInfo("global", "", line_number, line_number)

    line_number = max(1, min(line_number, len(lines)))

    if language == "python":
        for start_idx in range(line_number - 1, -1, -1):
            match = re.match(
                r"^([ \t]*)(?:async\s+def|def)\s+([A-Za-z_]\w*)\s*\(",
                lines[start_idx],
            )
            if not match:
                continue

            base_indent = len(match.group(1).replace("\t", "    "))
            end_line = len(lines)

            for end_idx in range(start_idx + 1, len(lines)):
                candidate = lines[end_idx]
                stripped = candidate.strip()

                if not stripped:
                    continue

                candidate_indent = len(candidate[:len(candidate) - len(candidate.lstrip(" \t"))].replace("\t", "    "))

                if candidate_indent <= base_indent:
                    end_line = end_idx
                    break

            if start_idx + 1 <= line_number <= end_line:
                return ScopeInfo("function", match.group(2), start_idx + 1, end_line)

        return ScopeInfo("global", "", line_number, line_number)

    if language not in {
            "java",
            "javascript",
            "typescript",
            "go",
            "csharp",
            "cpp",
            "c",
            "php",
            "rust",
            "kotlin",
            "swift",
    }:
        return ScopeInfo("global", "", line_number, line_number)

    scope_kind = "method" if language in {"java", "csharp", "kotlin", "swift", "php"} else "function"

    for start_idx in range(line_number - 1, -1, -1):
        signature_parts = []
        open_brace_line = None

        for cursor in range(start_idx, min(len(lines), start_idx + 6)):
            stripped = lines[cursor].strip()

            if not stripped and not signature_parts:
                break

            signature_parts.append(stripped)

            if "{" in stripped:
                open_brace_line = cursor
                break

            if ";" in stripped:
                break

        if open_brace_line is None:
            continue

        signature_text = " ".join(signature_parts).strip()

        if "(" not in signature_text or ")" not in signature_text:
            continue

        if re.match(
                r"^(if|for|foreach|while|switch|catch|else|do|try|using|lock|with|synchronized)\b",
                signature_text,
        ):
            continue

        if re.search(r"\bnew\s+[A-Za-z_][\w$]*\s*\([^()]*\)\s*\{", signature_text):
            continue

        name_match = re.search(
            r"([A-Za-z_][\w$]*)\s*\([^()]*\)\s*(?:throws\b[^{}]*)?\{",
            signature_text,
        )

        if not name_match and language in {"javascript", "typescript"}:
            name_match = re.search(
                r"([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{",
                signature_text,
            )

        if not name_match:
            continue

        scope_name = name_match.group(1)
        if scope_name in {
                "if",
                "for",
                "foreach",
                "while",
                "switch",
                "catch",
                "else",
                "do",
                "try",
                "using",
                "lock",
                "with",
                "synchronized",
        }:
            continue

        brace_depth = 0
        entered_scope = False
        end_line = None

        for end_idx in range(open_brace_line, len(lines)):
            brace_depth += lines[end_idx].count("{")
            brace_depth -= lines[end_idx].count("}")

            if end_idx + 1 >= line_number and brace_depth > 0:
                entered_scope = True

            if brace_depth == 0:
                end_line = end_idx + 1
                break

        if entered_scope and end_line is not None and start_idx + 1 <= line_number <= end_line:
            return ScopeInfo(scope_kind, scope_name, start_idx + 1, end_line)

    return ScopeInfo("global", "", line_number, line_number)


def resolve_scope(filepath: str, line_number: int) -> ScopeInfo:
    language = detect_language(filepath)

    try:
        if language == "unknown":
            return ScopeInfo("global", "", line_number, line_number)

        return resolve_scope_with_parser(filepath, line_number, language)

    except Exception:
        return ScopeInfo("global", "", line_number, line_number)


# Issue normalization, grouping and deduplication helpers
def build_issue_key(issue: Issue) -> str:
    if issue.sonar_key and issue.sonar_key != "NO_KEY":
        return issue.sonar_key
    return f"{issue.file}:{issue.line}:{issue.target_name}:{issue.severity}"


def normalize_and_deduplicate_issues(issues: list[Issue]) -> tuple[list[Issue], int]:
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

        sonar_issue_key = build_issue_key(issue)

        if issue.sonar_key and issue.sonar_key != "NO_KEY":
            if sonar_issue_key in seen_sonar_keys:
                continue
            seen_sonar_keys.add(sonar_issue_key)

        prepared_issues.append(issue)

    return prepared_issues, dropped_invalid_issues


def build_group_key(issue: Issue) -> tuple[str, str, str]:
    return (
        issue.file,
        normalize_code_block(clean_replacement_text(issue.original_code or "")),
        normalize_code_block(clean_replacement_text(issue.proposed_code or "")),
    )


# Comment formatting and hidden tracking metadata helpers
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


def build_hidden_ids(issue_keys: list[str]) -> str:
    unique_keys = list(dict.fromkeys(k.strip() for k in issue_keys if k and k.strip()))
    if not unique_keys:
        return ""
    ids_lines = "\n".join(f"ID: {k}" for k in unique_keys)
    return f"<!-- CodeGuardian-IDs:\n{ids_lines}\n-->"


def wrap_agent_comment(body: str) -> str:
    return f"{CODEGUARDIAN_AGENT_MARKER}\n{body}"


def is_agent_comment(comment_text: str) -> bool:
    return CODEGUARDIAN_AGENT_MARKER in (comment_text or "")


def build_comment_content(issues: list[Issue]) -> str:
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

    issue_keys = list(dict.fromkeys(build_issue_key(i) for i in issues))

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
                f"{build_hidden_ids(issue_keys)}")
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
            f"{build_hidden_ids(issue_keys)}")

    return wrap_agent_comment(body)


# Webhook payload and repository context helpers
def load_webhook_data(filepath: str) -> tuple[str, str, str, str]:
    with open(filepath, "r") as file:
        data = json.load(file)

    project_key = data.get("project_key")
    pr_id = data.get("pr_id")
    repo_slug = data.get("repo_slug")
    workspace = data.get("workspace", "medinafdzz")

    if not project_key:
        logger.error("Project key not found in the JSON file.")
        return "", "", "", ""

    if not pr_id:
        logger.error("Pull request ID not found in the JSON file.")
        return "", "", "", ""

    if not repo_slug:
        logger.error("Repository slug not found in the JSON file.")
        return "", "", "", ""

    return project_key, str(pr_id), str(repo_slug), str(workspace)


def get_code_context(filepath: str, line_number: int, context_window: int = 20) -> str:
    try:
        lines = read_file_lines(filepath)

        # Calculate the start and end lines for the code snippet
        start_line = max(0, line_number - context_window - 1)  # -1 because line numbers are typically 1-indexed
        end_line = min(len(lines), line_number + context_window)

        snippet = []
        for i in range(start_line, end_line):
            # Here I mark visually the error line
            prefix = ">> " if (i + 1) == line_number else "   "
            snippet.append(f"{prefix}{i + 1}: {lines[i].rstrip()}")

        return "\n".join(snippet)
    except Exception as e:
        return f"Error reading code context: {e}"


# SonarQube integration helpers
def clean_sonar_results(raw_results: CallToolResult) -> list[dict]:
    content_text = raw_results.content[0].text
    issues_data = json.loads(content_text)

    issues_list = issues_data.get("issues", []) if isinstance(issues_data, dict) else issues_data
    if not isinstance(issues_list, list):
        raise ValueError("Unexpected SonarQube issues format")

    cleaned = []
    for issue in issues_list:
        cleaned.append({
            "sonar_key": issue.get("key", "NO_KEY"),
            "severity": issue.get("severity"),
            "message": issue.get("message"),
            "line": issue.get("textRange", {}).get("startLine", 0),
            "file": issue.get("component", "").split(":")[-1],
        })

    return cleaned


async def fetch_sonar_issues(project_key: str) -> list[dict]:

    # Configure the SonarQube parameters
    sonar_parameters = StdioServerParameters(
        command="docker",
        args=[
            "run",
            "-i",
            "--rm",
            "--init",
            "--pull=missing",
            "--network",
            "services-net",
            "-e",
            "SONARQUBE_URL=http://sonarqube-server:9000",
            "-e",
            f"SONARQUBE_TOKEN={os.getenv('SONARQUBE_AUTH_TOKEN')}",
            "mcp/sonarqube",
        ],
        # Pass the necessary environment variables for SonarQube authentication and URL
        env=os.environ.copy(),
    )

    # Start the SonarQube client session using mcp tools
    results = None

    try:
        async with stdio_client(sonar_parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # Call the SonarQube tool to search for issues in the specified project
                results = await session.call_tool(
                    name="search_sonar_issues_in_projects",
                    arguments={
                        "project_key": project_key,
                        "resolved": "false",  # Only analyze unresolved issues to avoid noise in the analysis
                        "inNewCodePeriod":
                            "true",  # Necessary bc it analyzes only the code changed in the pull request, no added issues from the other branch
                    },
                )
                if results:
                    logger.info(f"SonarQube analysis completed successfully for project {project_key}.")

    except Exception as e:
        logger.error(f"Failed to connect to SonarQube: {e}")
        raise AgentExecutionError("SonarQube connection failed") from e

    # Clean the SonarQube results to save tokens and optimize the prompt by the most critical issues
    try:
        all_issues = clean_sonar_results(results)
    except Exception as e:
        logger.error(f"Failed to parse SonarQube results: {e}")
        raise AgentExecutionError("Failed to parse SonarQube results") from e

    severity_order = {
        "BLOCKER": 0,
        "CRITICAL": 1,
        "HIGH": 1,
        "MAJOR": 2,
        "MEDIUM": 2,
        "MINOR": 3,
        "LOW": 3,
        "INFO": 4,
    }

    cleaned_issues = [issue for issue in all_issues if issue.get("severity") in severity_order]

    cleaned_issues.sort(key=lambda issue: (
        severity_order.get(issue.get("severity"), 99),
        issue.get("file", ""),
        issue.get("line", 0),
    ))

    max_issues = int(os.getenv("CODEGUARDIAN_MAX_ISSUES", "20"))
    top_issues = cleaned_issues[:max_issues]  # Limit the number of issues sent to the AI after sorting by severity

    for issue in top_issues:
        issue["code_context"] = get_code_context(issue["file"], issue["line"])

        scope = resolve_scope(issue["file"], issue["line"])
        issue["scope_kind"] = scope.kind
        issue["scope_name"] = scope.name
        issue["scope_start_line"] = scope.start_line
        issue["scope_end_line"] = scope.end_line

    return top_issues

# AI analysis and batching logic for model-generated fixes
def build_scope_batches(issues: list[dict]) -> list[list[dict]]:
    grouped: dict[tuple, list[dict]] = {}
    ordered_keys: list[tuple] = []

    for issue in sorted(
            issues,
            key=lambda item: (
                item.get("file", ""),
                int(item.get("scope_start_line", item.get("line", 0)) or 0),
                int(item.get("line", 0) or 0),
            ),
    ):
        scope_kind = issue.get("scope_kind", "global")
        scope_name = issue.get("scope_name", "")
        scope_start = int(issue.get("scope_start_line", issue.get("line", 0)) or issue.get("line", 0))
        scope_end = int(issue.get("scope_end_line", issue.get("line", 0)) or issue.get("line", 0))

        if scope_kind in {"function", "method"}:
            key = (issue.get("file", ""), scope_kind, scope_name, scope_start, scope_end)
        else:
            # Los problemas fuera de función van solos
            key = (
                issue.get("file", ""),
                "global",
                f"global:{issue.get('sonar_key', 'NO_KEY')}:{issue.get('line', 0)}",
                int(issue.get("line", 0) or 0),
                int(issue.get("line", 0) or 0),
            )

        if key not in grouped:
            grouped[key] = []
            ordered_keys.append(key)

        grouped[key].append(issue)

    return [grouped[key] for key in ordered_keys]


def analyze_code_with_gemini(project_key: str, issues: list[dict]) -> Decision:
    client = genai.Client(api_key=os.getenv("LLM_AUTH_TOKEN"))

    all_model_issues: list[Issue] = []
    total_prompt_tokens = 0
    total_response_tokens = 0
    total_tokens = 0
    start_time = time.time()

    decline_pr = any(str(issue.get("severity", "")).upper() in {"BLOCKER", "CRITICAL"} for issue in issues)

    batches = build_scope_batches(issues)

    for batch in batches:

        batch_scope_kind = batch[0].get("scope_kind", "global")
        batch_scope_name = batch[0].get("scope_name", "")
        batch_scope_start = int(batch[0].get("scope_start_line", batch[0].get("line", 0)) or batch[0].get("line", 0))
        batch_scope_end = int(batch[0].get("scope_end_line", batch[0].get("line", 0)) or batch[0].get("line", 0))

        if batch_scope_kind in {"function", "method"}:
            scope_instruction = f"""
            All findings in this batch belong to the same {batch_scope_kind}: '{batch_scope_name}'.
            This scope starts at line {batch_scope_start} and ends at line {batch_scope_end}.

            If a real code change is needed, return exactly one issue object for this whole scope.
            Consolidate all findings in the batch into one single refactor proposal when applicable.
            Use one original_code block and one proposed_code block covering the full scope when needed.
            Do not return multiple issue objects for the same function or method.
            If no real fix is needed, return an empty issues list.
            """
        else:
            scope_instruction = """
            This finding is outside any function or method.
            Treat it as a global or top-level issue.
            Return one issue object for this finding only.
            Do not merge it with any other scope.
            """

        prompt = f"""
            You are reviewing SonarQube findings from project '{project_key}'.

            {scope_instruction}

            Rules:
            - Keep the exact sonar_key from the input.
            - proposed_code must directly replace original_code.
            - Use the smallest valid replacement that compiles or parses in place.
            - Do not invent APIs, symbols, imports, or variables unless strictly required.
            - Keep formatting and indentation consistent with the code context.
            - original_code must be the full replaceable block if a larger block is needed.
            - Use real multiline code, not literal '\\n'.
            - If the code is already correct, already handled, already uses the proper construct, or no code change is required, return no issue for that finding.
            - Never return an issue when original_code and proposed_code would be identical.
            - Do not emit issues for false positives, already-fixed code, or findings that only justify an explanation without a code change.
            - Only return issues that require a real code modification.
            - proposed_code must contain only the replacement block for original_code.
            - Do not prepend or append import statements to proposed_code unless original_code itself includes the import section.
            - If the fix requires new imports outside the replaceable block, list them in required_imports.
            - required_imports must contain only concrete import lines exactly as they should appear in the file, for example: "import java.sql.PreparedStatement;".
            - If no additional imports are required, return an empty required_imports array.

            For function or method batches:
            - return exactly one combined issue object only if a real code change is needed
            - summarize all covered findings inside "problem" as bullet points
            - summarize all applied changes inside "solution" as bullet points
            - set original_start_line and original_end_line to cover the full scope
            - set original_code to the full scope before changes
            - set proposed_code to the full scope after applying all fixes
            - use the sonar_key of the first covered finding in the batch
            - merge and deduplicate all needed imports into required_imports
            - if no real fix is needed, return an empty issues list

            Return ONLY valid JSON with this shape:
            {{
              "issues": [
                {{
                  "sonar_key": "...",
                  "file": "...",
                  "target_type": "...",
                  "target_name": "...",
                  "line": 0,
                  "original_start_line": 0,
                  "original_end_line": 0,
                  "problem": "...",
                  "severity": "...",
                  "solution": "...",
                  "original_code": "...",
                  "proposed_code": "...",
                  "required_imports": []
                }}
              ]
            }}

            SONARQUBE DATA:
            {json.dumps(batch)}
        """

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=IssueBatchDecision,
                temperature=0,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )

        try:
            total_prompt_tokens += int(response.usage_metadata.prompt_token_count)
            total_response_tokens += int(response.usage_metadata.candidates_token_count)
            total_tokens += int(response.usage_metadata.total_token_count)
        except Exception:
            pass

        try:
            partial_decision = IssueBatchDecision.model_validate_json(response.text)
        except Exception as e:
            logger.error(
                "Failed to parse Gemini batch response for sonar keys %s: %s",
                [issue.get("sonar_key", "NO_KEY") for issue in batch],
                e,
            )
            logger.error("The response from the model was: %s", response.text)
            continue

        expected_sonar_keys = {
            issue.get("sonar_key", "NO_KEY") for issue in batch if issue.get("sonar_key", "NO_KEY") != "NO_KEY"
        }

        kept_batch_issues: dict[str, Issue] = {}

        for issue in partial_decision.issues:
            if not issue.sonar_key or issue.sonar_key == "NO_KEY":
                continue
            if issue.sonar_key not in expected_sonar_keys:
                continue
            if issue.sonar_key in kept_batch_issues:
                continue
            kept_batch_issues[issue.sonar_key] = issue

        all_model_issues.extend(kept_batch_issues.values())

    duration = time.time() - start_time
    current_timestamp = time.time()

    registry = CollectorRegistry()
    latency = Gauge(
        "codeguardian_analysis_latency_seconds",
        "Response time of Gemini model (s)",
        registry=registry,
    )
    execution_time = Gauge(
        "codeguardian_last_execution_timestamp",
        "Timestamp of the last agent execution",
        registry=registry,
    )
    prompt_tokens = Gauge(
        "codeguardian_analysis_prompt_tokens",
        "Tokens used in the prompt sent to Gemini model",
        registry=registry,
    )
    response_tokens = Gauge(
        "codeguardian_analysis_response_tokens",
        "Tokens used in the response received",
        registry=registry,
    )
    total_tokens_metric = Gauge(
        "codeguardian_analysis_total_tokens",
        "Total tokens used in the Gemini model response",
        registry=registry,
    )

    latency.set(duration)
    execution_time.set(current_timestamp)
    prompt_tokens.set(total_prompt_tokens)
    response_tokens.set(total_response_tokens)
    total_tokens_metric.set(total_tokens)

    try:
        build_id = os.getenv("BUILD_NUMBER", "local_build")
        event_type = "pull_request"
        display_label = f"{project_key}-PR-{build_id}"

        push_to_gateway(
            "pushgateway:9091",
            job="codeguardian_agent",
            grouping_key={
                "build_number": build_id,
                "event_type": event_type,
                "display_id": display_label,
                "repository": project_key,
                "exec_timestamp": str(int(current_timestamp)),
            },
            registry=registry,
        )

    except Exception as metric_error:
        logger.error(f"Failed to push metrics to Prometheus Pushgateway: {metric_error}")

    logger.info("Gemini produced %s issues", len(all_model_issues))

    return Decision(
        decline_pr=decline_pr,
        issues=all_model_issues,
        comment=f"Generated fixes for {len(all_model_issues)} Sonar findings.",
    )


# Pull request comment readers using Atlassian Rovo MCP
async def get_pull_request_comments(
    session: ClientSession,
    pr_id: str,
    repo_slug: str,
    workspace: str,
) -> list[dict]:
    try:
        results = await session.call_tool(
            name="bitbucketPullRequest",
            arguments={
                "action": "comments",
                "workspaceId": workspace,
                "repoId": repo_slug,
                "prId": int(pr_id),
                "pagelen": 100,
            },
        )

        comments_data = json.loads(results.content[0].text)

        if isinstance(comments_data, dict):
            if isinstance(comments_data.get("values"), list):
                return comments_data.get("values", [])
            if isinstance(comments_data.get("comments"), list):
                return comments_data.get("comments", [])

        if isinstance(comments_data, list):
            return comments_data

        return []
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


async def get_inline_comments(session: ClientSession, pr_id: str, repo_slug: str, workspace: str) -> dict[int, dict]:
    try:
        comments = await get_pull_request_comments(session, pr_id, repo_slug, workspace)

        active_inline_comments = {}

        for comment in comments:
            if comment.get("deleted", False):
                continue

            if comment.get("parent"):
                continue

            raw_text = (comment.get("content", {}) or {}).get("raw", "") or comment.get("content", "")

            if not is_agent_comment(raw_text):
                continue

            issue_keys = extract_issue_key(raw_text)

            if not issue_keys:
                continue

            comment_id = int(comment.get("id"))
            resolved = comment.get("resolved", False)
            inline_data = comment.get("inline") or {}
            outdated = bool(inline_data.get("outdated", False))

            active_inline_comments[comment_id] = {
                "comment_id": comment_id,
                "resolved": resolved,
                "inline": inline_data,
                "outdated": outdated,
                "issue_keys": set(issue_keys),
                "raw_text": raw_text,
            }

        return active_inline_comments

    except Exception as e:
        logger.error(f"Failed to retrieve inline comments: {e}")
        raise


# Bitbucket REST helpers for inline comment creation and deletion
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
        return False
    except Exception:
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
        return False
    except Exception:
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
            await asyncio.sleep(0.2)
        else:
            failed_comment_ids.add(comment_id)
            logger.info("Comment %s could not be deleted", comment_id)

    return deleted_comment_ids, failed_comment_ids


# Pull request synchronization and final reporting workflow
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

        line_end = max(int(getattr(i, "original_end_line", i.line) or i.line) for i in issues)
        content = build_comment_content(issues)

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
) -> int:
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

        same_group = (build_group_key(issue) == build_group_key(last_issue) and issue_start <= last_end + merge_gap)

        if same_group:
            current_group.append(issue)
        else:
            grouped_issues.append(current_group)
            current_group = [issue]

    if current_group:
        grouped_issues.append(current_group)

    existing_comment_ids = set(active_inline_comments.keys())
    if existing_comment_ids:
        await delete_comment_ids(pr_id, repo_slug, workspace, existing_comment_ids)

    created_comments = 0

    for issue_group in reversed(grouped_issues):
        created = await post_issue_group_comment(pr_id, repo_slug, issue_group, workspace)
        if created:
            created_comments += 1
        await asyncio.sleep(0.2)

    return created_comments


async def report_to_bitbucket(
    pr_id: str,
    repo_slug: str,
    workspace: str,
    decision: Decision,
) -> None:
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

            await synchronize_inline_comments(
                session_bb,
                pr_id,
                repo_slug,
                workspace,
                decision.issues,
            )

            logger.info("Comments synchronized")

    except Exception as e:
        logger.error(f"Failed to report analysis results to Bitbucket: {e}")
        raise AgentExecutionError("Bitbucket reporting failed") from e


# Main orchestration entry point
async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Path to the JSON file to analyze")
    args = parser.parse_args()

    project_key, pr_id, repo_slug, workspace = load_webhook_data(args.file)
    if not project_key or not pr_id or not repo_slug:
        logger.error("Project key, pull request ID or repo slug not found in the JSON file.")
        raise AgentExecutionError("Webhook payload is missing required fields")

    issues = await fetch_sonar_issues(project_key)

    if not issues:
        logger.info("No relevant issues found by SonarQube. Reporting clean analysis state to the pull request.")
        auto_decision = Decision(
            decline_pr=False,
            issues=[],
            comment=
            "CodeGuardian analyzed this pull request and did not detect any relevant issues in the modified code.",
        )

        await report_to_bitbucket(pr_id, repo_slug, workspace, auto_decision)
        return

    logger.info(f"Relevant issues found by SonarQube: {len(issues)}. Proceeding with AI analysis.")

    decision = analyze_code_with_gemini(project_key, issues)

    decision.issues, dropped_invalid_issues = normalize_and_deduplicate_issues(decision.issues)

    decision.decline_pr = any(issue.severity in {"BLOCKER", "CRITICAL"} for issue in decision.issues)

    if dropped_invalid_issues:
        logger.info("Dropped %s invalid issues", dropped_invalid_issues)

    await report_to_bitbucket(pr_id, repo_slug, workspace, decision)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AgentExecutionError as e:
        logger.error(f"Agent execution failed: {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Unexpected unhandled error: {e}")
        sys.exit(1)
