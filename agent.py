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
from mcp.types import CallToolResult  # Library to manage the results of the MCP tools
from google.genai import (
    types,)  # Library to manage the configuration and types for the Gemini model API
from mcp import (
    ClientSession,
    StdioServerParameters,
)  # Library to manage the client session with the MCP tools
from mcp.client.stdio import (
    stdio_client,)  # Library to interact with the MCP tools using standard input/output
from pydantic import BaseModel  # Library for json formating and validation
from prometheus_client import (
    CollectorRegistry,
    Gauge,
    push_to_gateway,
)

# Keep Jenkins logs readable without hiding useful info when something breaks.
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


# These models are the data the agent moves around between Sonar, Gemini and Bitbucket.class
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


class Decision(BaseModel):
    decline_pr: bool
    issues: list[Issue]
    comment: str

class IssueBatchDecision(BaseModel):
    issues: list[Issue]

class AgentExecutionError(Exception):
    """Raised when the agent cannot complete a required execution step."""


CODEGUARDIAN_SUMMARY_TITLE = "**CodeGuardian Analysis Summary**"
CODEGUARDIAN_AGENT_MARKER = "<!-- CodeGuardian-Agent -->"


# Generate a key for each issue
def build_issue_key(issue: Issue) -> str:
    if issue.sonar_key and issue.sonar_key != "NO_KEY":
        return issue.sonar_key
    return f"{issue.file}:{issue.line}:{issue.target_name}:{issue.severity}"


# Clean, validate, and deduplicate the issues returned by the model
def normalize_and_deduplicate_issues(issues: list[Issue]) -> tuple[list[Issue], int]:
    prepared_issues = []
    dropped_invalid_issues = 0
    seen_sonar_keys = set()
    seen_semantic_keys = set()

    for issue in issues:
        issue.file = (issue.file or "").strip()
        issue.target_type = (issue.target_type or "").strip()
        issue.target_name = (issue.target_name or "").strip()
        issue.problem = (issue.problem or "").strip()
        issue.severity = (issue.severity or "").strip().upper()
        issue.solution = (issue.solution or "").strip()
        issue.original_code = clean_replacement_text(issue.original_code or "")
        issue.proposed_code = clean_replacement_text(issue.proposed_code or "")

        if issue.line < 1:
            issue.line = 1

        if issue.original_start_line is not None and issue.original_start_line < 1:
            issue.original_start_line = 1

        if issue.original_end_line is not None and issue.original_end_line < 1:
            issue.original_end_line = 1

        if issue.original_start_line and issue.original_end_line:
            if issue.original_end_line < issue.original_start_line:
                issue.original_start_line, issue.original_end_line = issue.original_end_line, issue.original_start_line

        if (not issue.file or not issue.problem or not issue.solution or not issue.severity or
                not issue.original_code or not issue.proposed_code):
            dropped_invalid_issues += 1
            continue

        sonar_issue_key = build_issue_key(issue)
        semantic_issue_key = (
            issue.file,
            int(getattr(issue, "line", 0) or 0),
            issue.problem.lower(),
            issue.severity.upper(),
            normalize_code_block(issue.original_code),
            normalize_code_block(issue.proposed_code),
        )

        if issue.sonar_key and issue.sonar_key != "NO_KEY":
            if sonar_issue_key in seen_sonar_keys:
                continue
            seen_sonar_keys.add(sonar_issue_key)
            prepared_issues.append(issue)
            continue

        if semantic_issue_key in seen_semantic_keys:
            continue

        seen_semantic_keys.add(semantic_issue_key)
        prepared_issues.append(issue)

    return prepared_issues, dropped_invalid_issues


# Group issues that share the same replacement and solution block
def build_group_key(issue: Issue) -> tuple[str, str, str, str]:
    return (
        issue.file,
        normalize_code_block(clean_replacement_text(issue.original_code)),
        normalize_code_block(clean_replacement_text(issue.proposed_code)),
        normalize_code_block((issue.solution or "").strip().lower()),
    )


# Build the final comment body for one issue or a grouped set of issues
def build_comment_content(issues: list[Issue]) -> str:
    if not issues:
        return ""

    issues = sorted(
        issues,
        key=lambda i: (
            int(getattr(i, "original_start_line", i.line)),
            int(getattr(i, "original_end_line", i.line)),
            i.severity,
            i.problem,
        ),
    )

    base_issue = max(
        issues,
        key=lambda i: int(getattr(i, "original_end_line", i.line)) - int(getattr(i, "original_start_line", i.line)),
    )

    file_extension = base_issue.file.split(".")[-1] if "." in base_issue.file else "txt"
    clean_orig = clean_replacement_text(base_issue.original_code)
    clean_prop = clean_replacement_text(base_issue.proposed_code)

    min_line = min(int(getattr(i, "original_start_line", i.line)) for i in issues)
    max_line = max(int(getattr(i, "original_end_line", i.line)) for i in issues)
    issue_keys = list(dict.fromkeys(build_issue_key(i) for i in issues))

    if len(issues) == 1:
        issue = issues[0]
        body = (
            f"### Code Issue\n\n"
            f"**File:** {issue.file}\n\n"
            f"**Lines:** {min_line}-{max_line}\n\n"
            f"**Problem ({issue.severity}):** {issue.problem}\n\n"
            f"**Solution:** {issue.solution}\n\n"
            f"**Block to substitute:**\n"
            f"```{file_extension}\n"
            f"{clean_orig}\n"
            f"```\n\n"
            f"**Refactored Code:**\n"
            f"```{file_extension}\n"
            f"{clean_prop}\n"
            f"```\n\n"
            f"{build_hidden_ids(issue_keys)}"
        )
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
        solution_block = f"**Suggested solution:** {unique_solutions[0]}\n\n"
    else:
        solution_block = "**Suggested actions:**\n\n" + "\n".join(
            f"- {solution}" for solution in unique_solutions
        ) + "\n\n"

    body = (
        f"### Code Issues\n\n"
        f"**File:** {base_issue.file}\n\n"
        f"**Lines:** {min_line}-{max_line}\n\n"
        f"**Detected problems:**\n\n"
        f"{combined_problems}\n\n"
        f"{solution_block}"
        f"**Block to substitute:**\n"
        f"```{file_extension}\n"
        f"{clean_orig}\n"
        f"```\n\n"
        f"**Refactored Code:**\n"
        f"```{file_extension}\n"
        f"{clean_prop}\n"
        f"```\n\n"
        f"{build_hidden_ids(issue_keys)}"
    )

    return wrap_agent_comment(body)

# Publish one inline comment for a whole issue group
async def post_issue_group_comment(
    session: ClientSession,
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
            key=lambda i: int(getattr(i, "original_end_line", i.line)) - int(getattr(i, "original_start_line", i.line)),
        )

        if not is_valid_replacement(base_issue):
            return False

        if not validate_issue_against_file(base_issue):
            return False

        line_end = max(int(getattr(i, "original_end_line", i.line)) for i in issues)
        content = build_comment_content(issues)

        await session.call_tool(
            name="addPullRequestComment",
            arguments={
                "workspace": workspace,
                "pull_request_id": int(pr_id),
                "repo_slug": repo_slug,
                "content": content,
                "inline": {
                    "path": base_issue.file,
                    "to": line_end,
                },
            },
        )
        return True
    except Exception as e:
        logger.error(f"Failed to add inline comment: {e}")
        raise


# Read the hidden IDs stored inside existing agent comments
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


# Store issue IDs inside a hidden block for later tracking
def build_hidden_ids(issue_keys: list[str]) -> str:
    unique_keys = list(dict.fromkeys(k.strip() for k in issue_keys if k and k.strip()))
    if not unique_keys:
        return ""
    ids_lines = "\n".join(f"ID: {k}" for k in unique_keys)
    return f"<!-- CodeGuardian-IDs:\n{ids_lines}\n-->"


# Add a hidden marker so the agent can recognize its own comments
def wrap_agent_comment(body: str) -> str:
    return f"{CODEGUARDIAN_AGENT_MARKER}\n{body}"


# Check whether a comment was created by the agent
def is_agent_comment(comment_text: str) -> bool:
    return CODEGUARDIAN_AGENT_MARKER in (comment_text or "")


# Read the basic pull request data from the webhook payload
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


# Read file lines once and keep them cached for repeated checks
@lru_cache(maxsize=256)
def read_file_lines(filepath: str) -> list[str]:
    if not os.path.exists(filepath):
        raise FileNotFoundError("File not found.")
    with open(filepath, "r", encoding="utf-8") as file:
        return file.readlines()


# Return a code window around the reported line for model context
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


# Keep only the fields needed from the SonarQube response
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


# Fetch the most relevant unresolved issues from new code only
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

    return top_issues


# Ask the model for fixes and collect execution metrics
def analyze_code_with_gemini(project_key: str, issues: list[dict]) -> Decision:
    client = genai.Client(api_key=os.getenv("LLM_AUTH_TOKEN"))

    all_model_issues: list[Issue] = []
    total_prompt_tokens = 0
    total_response_tokens = 0
    total_tokens = 0
    start_time = time.time()

    decline_pr = any(
        str(issue.get("severity", "")).upper() in {"BLOCKER", "CRITICAL"}
        for issue in issues
    )

    batch_size = int(os.getenv("CODEGUARDIAN_BATCH_SIZE", "3"))
    line_gap = int(os.getenv("CODEGUARDIAN_BATCH_LINE_GAP", "25"))

    sorted_issues = sorted(
        issues,
        key=lambda issue: (
            issue.get("file", ""),
            int(issue.get("line", 0) or 0),
        ),
    )

    batches: list[list[dict]] = []
    current_batch: list[dict] = []

    for sonar_issue in sorted_issues:
        if not current_batch:
            current_batch.append(sonar_issue)
            continue

        previous_issue = current_batch[-1]
        same_file = sonar_issue.get("file", "") == previous_issue.get("file", "")
        close_lines = abs(int(sonar_issue.get("line", 0) or 0) - int(previous_issue.get("line", 0) or 0)) <= line_gap

        if same_file and close_lines and len(current_batch) < batch_size:
            current_batch.append(sonar_issue)
        else:
            batches.append(current_batch)
            current_batch = [sonar_issue]

    if current_batch:
        batches.append(current_batch)

    logger.info("Gemini batching plan: %s batches for %s Sonar findings", len(batches), len(issues))

    for batch in batches:
        prompt = f"""
        You are reviewing a small batch of SonarQube findings from project '{project_key}'.

        Analyze only the findings below.
        Return one issue object per input finding when a safe fix is possible.
        Do not merge multiple findings into one issue object.
        If several findings share the same fix, keep them as separate issue objects and reuse the same original_code, proposed_code, and solution.
        If no safe valid fix is possible for a finding, omit it.

        Rules:
        - Keep the exact sonar_key from the input.
        - proposed_code must directly replace original_code.
        - Use the smallest valid replacement that compiles or parses in place.
        - Do not invent APIs, symbols, imports, or variables unless strictly required.
        - Keep formatting and indentation consistent with the code context.
        - original_code must be the full replaceable block if a larger block is needed.
        - Use real multiline code, not literal '\\n'.

        Return ONLY valid JSON with this shape:
        {{"issues": [ ... ]}}

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
            issue.get("sonar_key", "NO_KEY")
            for issue in batch
            if issue.get("sonar_key", "NO_KEY") != "NO_KEY"
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

        missing_batch_keys = sorted(expected_sonar_keys - set(kept_batch_issues.keys()))
        if missing_batch_keys:
            logger.info("Gemini returned no actionable issue for sonar keys: %s", missing_batch_keys)

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
        logger.info(
            "Metrics pushed: build=%s latency=%.2fs total_tokens=%s",
            build_id,
            duration,
            total_tokens,
        )
    except Exception as metric_error:
        logger.error(f"Failed to push metrics to Prometheus Pushgateway: {metric_error}")

    logger.info(
        "Gemini returned actionable fixes for %s of %s Sonar findings",
        len(all_model_issues),
        len(issues),
    )

    return Decision(
        decline_pr=decline_pr,
        issues=all_model_issues,
        comment=f"Generated fixes for {len(all_model_issues)} Sonar findings.",
    )

# Load all pull request comments so they can be inspected
async def get_pull_request_comments(
    session: ClientSession,
    pr_id: str,
    repo_slug: str,
    workspace: str,
) -> list[dict]:
    try:
        results = await session.call_tool(
            name="getPullRequestComments",
            arguments={
                "workspace": workspace,
                "pull_request_id": int(pr_id),
                "repo_slug": repo_slug,
                "all": True,
            },
        )

        comments_data = json.loads(results.content[0].text)

        if isinstance(comments_data, dict):
            return comments_data.get("values", [])
        if isinstance(comments_data, list):
            return comments_data

        return []
    except Exception as e:
        logger.error(f"Failed to retrieve pull request comments: {e}")
        raise


# Find old top-level summary comments created by the agent
def get_agent_summary_comment_ids(comments: list[dict]) -> set[int]:
    summary_comment_ids: set[int] = set()

    for comment in comments:
        if comment.get("deleted", False):
            continue

        if comment.get("parent"):
            continue

        raw_text = (comment.get("content", {}) or {}).get("raw", "") or ""
        normalized_text = raw_text.replace(CODEGUARDIAN_AGENT_MARKER, "", 1).strip()

        if (is_agent_comment(raw_text) or normalized_text.startswith(CODEGUARDIAN_SUMMARY_TITLE) or
                raw_text.strip().startswith(CODEGUARDIAN_SUMMARY_TITLE)):
            if normalized_text.startswith(CODEGUARDIAN_SUMMARY_TITLE) or raw_text.strip().startswith(
                    CODEGUARDIAN_SUMMARY_TITLE):
                comment_id = comment.get("id")
                if comment_id is not None:
                    summary_comment_ids.add(int(comment_id))

    return summary_comment_ids


# Normalize raw replacement text returned by the model
def clean_replacement_text(value: str) -> str:
    return value.replace('\\n', '\n').strip('`').strip()


# Make sure the suggested replacement is not empty and actually changes the code.
def is_valid_replacement(issue: Issue) -> bool:
    clean_orig = clean_replacement_text(issue.original_code)
    clean_prop = clean_replacement_text(issue.proposed_code)
    return bool(clean_orig and clean_prop and clean_orig != clean_prop)


# Normalize code blocks before comparing them
def normalize_code_block(text: str) -> str:
    if not text:
        return ""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().splitlines()).strip()


# Check whether the model output matches the real file content closely enough.
def validate_issue_against_file(issue: Issue, line_tolerance: int = 20) -> bool:
    try:
        lines = read_file_lines(issue.file)

        start = int(getattr(issue, "original_start_line", issue.line) or issue.line)
        end = int(getattr(issue, "original_end_line", issue.line) or issue.line)

        if start < 1:
            start = 1
        if end < start:
            end = start
        if end > len(lines):
            end = len(lines)

        original = normalize_code_block(clean_replacement_text(issue.original_code))
        proposed = normalize_code_block(clean_replacement_text(issue.proposed_code))

        if not original or not proposed or original == proposed:
            return False

        exact_block = normalize_code_block("".join(lines[start - 1:end]))
        if original == exact_block:
            return True

        if original in exact_block or exact_block in original:
            return True

        window_start = max(1, start - line_tolerance)
        window_end = min(len(lines), end + line_tolerance)
        nearby_block = normalize_code_block("".join(lines[window_start - 1:window_end]))

        if original == nearby_block:
            return True

        if original in nearby_block or nearby_block in original:
            return True

        original_compact = re.sub(r"\s+", "", original)
        exact_compact = re.sub(r"\s+", "", exact_block)
        nearby_compact = re.sub(r"\s+", "", nearby_block)

        if original_compact == exact_compact or original_compact in exact_compact or exact_compact in original_compact:
            return True

        if original_compact == nearby_compact or original_compact in nearby_compact or nearby_compact in original_compact:
            return True

        logger.info(
            "File match failed for issue key=%s file=%s start=%s end=%s sonar_line=%s",
            build_issue_key(issue),
            issue.file,
            start,
            end,
            issue.line,
        )
        logger.info("Original code returned by model:\n%s", issue.original_code)
        logger.info("Exact block from file:\n%s", exact_block)
        logger.info("Nearby block from file:\n%s", nearby_block)

        return False

    except Exception as e:
        logger.info(
            "Exception validating issue against file key=%s file=%s: %s",
            build_issue_key(issue),
            getattr(issue, "file", ""),
            e,
        )
        return False


# Load the current inline comments created by the agent
async def get_inline_comments(session: ClientSession, pr_id: str, repo_slug: str, workspace: str) -> dict[int, dict]:
    try:
        results = await session.call_tool(
            name="getPullRequestComments",
            arguments={
                "workspace": workspace,
                "pull_request_id": int(pr_id),
                "repo_slug": repo_slug,
                "all": True,
            },
        )

        comments_data = json.loads(results.content[0].text)

        if isinstance(comments_data, dict):
            comments = comments_data.get("values", [])
        elif isinstance(comments_data, list):
            comments = comments_data
        else:
            comments = []

        active_inline_comments = {}

        for comment in comments:
            if comment.get("deleted", False):
                continue

            if comment.get("parent"):
                continue

            raw_text = comment.get("content", {}).get("raw", "")

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


# Build the auth headers for direct Bitbucket REST calls
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


# Return the base Bitbucket API URL used by REST helpers
def get_bitbucket_api_base_url() -> str:
    return os.getenv("BITBUCKET_URL", "https://api.bitbucket.org/2.0").rstrip("/")


# Build the REST URL for a pull request comment
def build_pullrequest_comment_url(
    pr_id: str,
    repo_slug: str,
    comment_id: str,
    workspace: str,
) -> str:
    base_url = get_bitbucket_api_base_url()
    return (f"{base_url}/repositories/{workspace}/{repo_slug}"
            f"/pullrequests/{pr_id}/comments/{comment_id}")


# Delete one inline comment directly through the REST API
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


# Delete a set of comments and track which ones failed
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


# Remove old summary comments that should no longer stay in the PR
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


# Reconcile the current analysis with the current PR comments
async def synchronize_inline_comments(
    session: ClientSession,
    pr_id: str,
    repo_slug: str,
    workspace: str,
    issues: list[Issue],
    preserved_issue_keys: set[str] | None = None,
) -> int:
    
    preserved_issue_keys = preserved_issue_keys or set()
    
    active_inline_comments = await get_inline_comments(session, pr_id, repo_slug, workspace)

    total_detected = len(issues)
    invalid_replacements = 0
    invalid_file_matches = 0
    valid_issues = []

    for issue in issues:
        if not is_valid_replacement(issue):
            invalid_replacements += 1
            continue
        if not validate_issue_against_file(issue):
            invalid_file_matches += 1
            continue
        valid_issues.append(issue)

    desired_issues_by_key = {build_issue_key(issue): issue for issue in valid_issues}
    desired_groups: dict[tuple[str, str, str, str], list[Issue]] = {}

    for issue in valid_issues:
        desired_groups.setdefault(build_group_key(issue), []).append(issue)

    logger.info(
        "Issues detected=%s, publishable=%s, skipped_invalid_replacement=%s, skipped_file_mismatch=%s, tracked_agent_comments=%s",
        total_detected,
        len(desired_issues_by_key),
        invalid_replacements,
        invalid_file_matches,
        len(active_inline_comments),
    )

    comments_to_delete: set[int] = set()
    existing_comments_by_group_key: dict[tuple[str, str, str, str], dict] = {}

    for comment_id in sorted(active_inline_comments.keys(), reverse=True):
        comment_info = active_inline_comments[comment_id]
        issue_keys = set(comment_info.get("issue_keys", set()))

        if (
            issue_keys
            and issue_keys.issubset(preserved_issue_keys)
            and not comment_info.get("resolved", False)
            and not comment_info.get("outdated", False)
        ):
            continue

        matching_issues = [desired_issues_by_key[k] for k in issue_keys if k in desired_issues_by_key]

        if not matching_issues:
            comments_to_delete.add(comment_id)
            continue

        group_key = build_group_key(
            max(
                matching_issues,
                key=lambda i: int(getattr(i, "original_end_line", i.line)) - int(
                    getattr(i, "original_start_line", i.line)),
            ))

        if group_key in existing_comments_by_group_key:
            comments_to_delete.add(comment_id)
            continue

        existing_comments_by_group_key[group_key] = comment_info

    deleted_comment_ids, failed_comment_ids = await delete_comment_ids(
        pr_id,
        repo_slug,
        workspace,
        comments_to_delete,
    )

    if deleted_comment_ids:
        logger.info(
            "Deleted %s duplicate/grouped legacy comments from PR %s",
            len(deleted_comment_ids),
            pr_id,
        )

    blocked_group_keys: set[tuple[str, str, str, str]] = set()

    for group_key, comment_info in existing_comments_by_group_key.items():
        comment_id = int(comment_info["comment_id"])

        if comment_id in deleted_comment_ids:
            continue

        if comment_id in failed_comment_ids:
            blocked_group_keys.add(group_key)
            continue

        inline_data = comment_info.get("inline") or {}
        desired_group = desired_groups.get(group_key)

        should_delete = False

        if desired_group is None:
            should_delete = True
        elif comment_info.get("resolved", False):
            should_delete = True
        elif comment_info.get("outdated", False):
            should_delete = True
        else:
            base_issue = max(
                desired_group,
                key=lambda i: int(getattr(i, "original_end_line", i.line)) - int(
                    getattr(i, "original_start_line", i.line)),
            )
            expected_path = base_issue.file
            expected_line = max(int(getattr(i, "original_end_line", i.line)) for i in desired_group)
            expected_raw_text = build_comment_content(desired_group).strip()
            current_raw_text = (comment_info.get("raw_text") or "").strip()

            if inline_data.get("path") != expected_path or inline_data.get("to") != expected_line:
                should_delete = True
            elif current_raw_text != expected_raw_text:
                should_delete = True

        if should_delete:
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
                logger.info("Comment %s could not be deleted", comment_id)
                blocked_group_keys.add(group_key)

    existing_group_keys_after_cleanup = {
        group_key for group_key, comment_info in existing_comments_by_group_key.items()
        if int(comment_info["comment_id"]) not in deleted_comment_ids
    }

    created_comments = 0

    for group_key, issue_group in desired_groups.items():
        if group_key in existing_group_keys_after_cleanup:
            continue

        if group_key in blocked_group_keys:
            continue

        created = await post_issue_group_comment(session, pr_id, repo_slug, issue_group, workspace)
        if created:
            created_comments += 1

        await asyncio.sleep(0.2)

    logger.info(
        "Comment sync result for PR %s: deleted_comments=%s, blocked_group_keys=%s, created_comments=%s",
        pr_id,
        len(deleted_comment_ids),
        len(blocked_group_keys),
        created_comments,
    )

    return created_comments


# Open the Bitbucket session and publish the final analysis state
async def report_to_bitbucket(
    pr_id: str,
    repo_slug: str,
    workspace: str,
    decision: Decision,
    preserved_issue_keys: set[str] | None = None,
) -> None:
    bitbucket_username = (os.getenv("BITBUCKET_EMAIL") or os.getenv("BITBUCKET_USERNAME") or "")

    bitbucket_password = (os.getenv("BITBUCKET_API_TOKEN") or os.getenv("BITBUCKET_APP_TOKEN") or "")

    bitbucket_env = os.environ.copy()
    bitbucket_env.update({
        "BITBUCKET_URL": os.getenv("BITBUCKET_URL", "https://api.bitbucket.org/2.0"),
        "BITBUCKET_WORKSPACE": workspace,
        "BITBUCKET_USERNAME": bitbucket_username,
        "BITBUCKET_PASSWORD": bitbucket_password,
    })

    if not pr_id or str(pr_id).lower() == "null":
        logger.error("No valid pull request ID was provided.")
        raise AgentExecutionError("Missing pull request ID")

    try:
        async with stdio_client(StdioServerParameters(
                command="bitbucket-mcp",
                args=[],
                env=bitbucket_env,
        )) as (read, write):
            async with ClientSession(read, write) as session_bb:
                await session_bb.initialize()

                deleted_summary_comments = await delete_agent_summary_comments(
                    session_bb,
                    pr_id,
                    repo_slug,
                    workspace,
                )

                created_inline_comments = await synchronize_inline_comments(
                    session_bb,
                    pr_id,
                    repo_slug,
                    workspace,
                    decision.issues,
                    preserved_issue_keys,
                )

                logger.info(
                    "Analysis state synchronized for PR %s: detected_issues=%s, created_inline_comments=%s, deleted_summary_comments=%s",
                    pr_id,
                    len(decision.issues),
                    created_inline_comments,
                    deleted_summary_comments,
                )

    except Exception as e:
        logger.error(f"Failed to report analysis results to Bitbucket: {e}")
        raise AgentExecutionError("Bitbucket reporting failed") from e


# Orchestrate the full flow from webhook input to PR update.
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

    current_sonar_keys = {
        issue.get("sonar_key", "NO_KEY")
        for issue in issues
        if issue.get("sonar_key", "NO_KEY") != "NO_KEY"
    }

    bitbucket_username = (os.getenv("BITBUCKET_EMAIL") or os.getenv("BITBUCKET_USERNAME") or "")
    bitbucket_password = (os.getenv("BITBUCKET_API_TOKEN") or os.getenv("BITBUCKET_APP_TOKEN") or "")

    bitbucket_env = os.environ.copy()
    bitbucket_env.update({
        "BITBUCKET_URL": os.getenv("BITBUCKET_URL", "https://api.bitbucket.org/2.0"),
        "BITBUCKET_WORKSPACE": workspace,
        "BITBUCKET_USERNAME": bitbucket_username,
        "BITBUCKET_PASSWORD": bitbucket_password,
    })

    preserved_issue_keys: set[str] = set()

    try:
        async with stdio_client(StdioServerParameters(
                command="bitbucket-mcp",
                args=[],
                env=bitbucket_env,
        )) as (read, write):
            async with ClientSession(read, write) as session_bb:
                await session_bb.initialize()
                active_inline_comments = await get_inline_comments(session_bb, pr_id, repo_slug, workspace)

                for comment_info in active_inline_comments.values():
                    if comment_info.get("resolved", False):
                        continue
                    if comment_info.get("outdated", False):
                        continue

                    for issue_key in comment_info.get("issue_keys", set()):
                        if issue_key in current_sonar_keys:
                            preserved_issue_keys.add(issue_key)

    except Exception as e:
        logger.warning("Could not preload existing agent comments: %s", e)

    issues_to_analyze = [
        issue
        for issue in issues
        if issue.get("sonar_key", "NO_KEY") not in preserved_issue_keys
    ]

    logger.info(
        "Skipping Gemini for %s Sonar findings already covered by active agent comments",
        len(preserved_issue_keys),
    )
    logger.info(
        "Sending %s Sonar findings to Gemini",
        len(issues_to_analyze),
    )

    if issues_to_analyze:
        decision = analyze_code_with_gemini(project_key, issues_to_analyze)
    else:
        decision = Decision(
            decline_pr=any(
                str(issue.get("severity", "")).upper() in {"BLOCKER", "CRITICAL"}
                for issue in issues
            ),
            issues=[],
            comment="No new Sonar findings needed fresh AI analysis.",
        )

    input_sonar_keys = {
        issue.get("sonar_key", "NO_KEY")
        for issue in issues_to_analyze
        if issue.get("sonar_key", "NO_KEY") != "NO_KEY"
    }
    returned_sonar_keys = {
        issue.sonar_key
        for issue in decision.issues
        if issue.sonar_key and issue.sonar_key != "NO_KEY"
    }
    missing_sonar_keys = sorted(input_sonar_keys - returned_sonar_keys)

    logger.info(
        "Coverage after Gemini: returned=%s of %s Sonar findings",
        len(returned_sonar_keys),
        len(input_sonar_keys),
    )

    if missing_sonar_keys:
        logger.info("Missing Sonar issue keys after Gemini: %s", missing_sonar_keys)

    decision.issues, dropped_invalid_issues = normalize_and_deduplicate_issues(decision.issues)

    if dropped_invalid_issues:
        logger.info("Dropped %s structurally invalid issues returned by Gemini", dropped_invalid_issues)

    logger.info(
        "AI analysis completed, reporting the results to Bitbucket. You can also check the detailed metrics of the analysis in Prometheus or Grafana."
    )

    await report_to_bitbucket(pr_id, repo_slug, workspace, decision, preserved_issue_keys)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AgentExecutionError as e:
        logger.error(f"Agent execution failed: {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Unexpected unhandled error: {e}")
        sys.exit(1)
