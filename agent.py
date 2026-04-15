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


class AgentExecutionError(Exception):
    """Raised when the agent cannot complete a required execution step."""


CODEGUARDIAN_SUMMARY_TITLE = "**CodeGuardian Analysis Summary**"
CODEGUARDIAN_AGENT_MARKER = "<!-- CodeGuardian-Agent -->"


# Generate a key for each issue
def build_issue_key(issue: Issue) -> str:
    if issue.sonar_key and issue.sonar_key != "NO_KEY":
        return issue.sonar_key
    return f"{issue.file}:{issue.line}:{issue.target_name}:{issue.severity}"


# In case the AI returns duplicated issues, we can filter them by their SonarQube key to avoid duplicated comments in Bitbucket.
def deduplicate_issues(issues: list[Issue]) -> list[Issue]:
    seen = set()
    unique_issues = []

    for issue in issues:
        issue_key = build_issue_key(issue)
        if issue_key in seen:
            continue
        seen.add(issue_key)
        unique_issues.append(issue)

    return unique_issues


def sanitize_issue(issue: Issue) -> Issue:
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

    return issue


def is_structurally_valid_issue(issue: Issue) -> bool:
    if not issue.file:
        return False
    if not issue.problem:
        return False
    if not issue.solution:
        return False
    if not issue.severity:
        return False
    if not issue.original_code:
        return False
    if not issue.proposed_code:
        return False
    return True


# Extract the hidden issue identifiers previously stored in agent comments.
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


# Attach stable hidden identifiers to each agent comment so the next analysis
# can map current issues to existing PR comments.
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


# Get the project key and pull request ID from the webhook input and leave them in a format the rest of the flow can reuse.
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


# Add nearby lines around the issue so the AI can understand the problem with some real context.
@lru_cache(maxsize=256)
def _read_file_lines(filepath: str) -> list[str]:
    if not os.path.exists(filepath):
        raise FileNotFoundError("File not found.")
    with open(filepath, "r", encoding="utf-8") as file:
        return file.readlines()


def get_code_context(filepath: str, line_number: int, context_window: int = 60) -> str:
    try:
        lines = _read_file_lines(filepath)

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


# Trim the Sonar response before sending it to the LLM so the prompt stays smaller and cleaner.
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
            "component": issue.get("component"),
            "message": issue.get("message"),
            "line": issue.get("textRange", {}).get("startLine", 0),
            "project": issue.get("project"),
            "file": issue.get("component", "").split(":")[-1],
        })

    return cleaned


# Pull only the serious unresolved issues from new code so old debt does not mix into this run.
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


# Ask Gemini for the final verdict once the most relevant Sonar findings have already been filtered.
def analyze_code_with_gemini(project_key: str, issues: list[dict]) -> Decision:
    # Configure the Gemini model parameters
    client = genai.Client(api_key=os.getenv("LLM_AUTH_TOKEN"))
    # Prompt for the model to analyze the SonarQube issues
    prompt = f"""
    Act as a Principal Software Architect, Static Analysis Expert, and Secure Code Reviewer.

    Analyze the following SonarQube technical findings for the project: '{project_key}'.

    ### GLOBAL OBJECTIVE
    For each finding, generate the safest and smallest valid code replacement that fixes the issue without breaking compilation, syntax, scope, control flow, or surrounding behavior.

    RULES FOR 'proposed_code'
    Return ONLY the exact raw code snippet that replaces the problematic region.
    DO NOT wrap the code in markdown blocks.
    DO NOT include explanations inside the code.
    Preserve the original programming language, coding style, naming style, formatting style, and indentation style of the provided code context.
    Prefer the smallest safe change, but NEVER sacrifice correctness for brevity.
    The replacement must be syntactically valid in the original language.
    The replacement must remain semantically coherent with the surrounding code context.
    DO NOT invent APIs, variables, functions, classes, modules, imports, types, constants, macros, or symbols that are not already present or clearly required.
    Only add imports, includes, using statements, dependencies, declarations, or boilerplate when they are strictly necessary for correctness.
    NEVER return a partial fragment if a larger replacement region is required for correctness.
    Preserve variable lifetime, visibility, ownership, mutability, initialization order, and valid scope.
    Preserve resource lifecycle semantics, cleanup semantics, and error-handling semantics when relevant.
    Preserve control flow correctness: do not leave unresolved branches, missing returns, broken loops, orphaned conditions, unreachable code, or unbalanced delimiters.
    If the finding is inside a syntactic structure such as try, catch, finally, if, else, for, while, switch, class, method, lambda, or block, original_code must include the whole smallest syntactic unit that remains valid after replacement.
    Do not introduce unresolved identifiers, dangling references, invalid state transitions, or invalid object/resource usage.
    Do not move declarations into a narrower scope if they are used later.
    Do not move statements outside the scope they depend on.
    Do not change behavior unrelated to the finding unless strictly necessary to produce a valid fix.
    When several nearby findings in the same file are solved by the exact same replacement region, generate the same replacement block for them.
    When a correct fix requires replacing a larger logical block, return that larger block instead of a smaller unsafe fragment.
    The final replacement must be production-ready.
    Assume the replacement will be directly applied over the original_code block, so it must compile, run, or parse correctly in that location.
    For interpreted languages, ensure the replacement is executable and structurally valid.
    For compiled languages, ensure the replacement is compilable in context.
    For memory/resource/concurrency issues, ensure the fix does not introduce leaks, deadlocks, race conditions, invalid ownership, double release, or scope misuse.
    Do not return placeholder text such as TODO, FIXME, pseudocode, ellipsis, or comments like "rest of code unchanged".
    MUST use physical line breaks for multiline code. DO NOT use literal '\n' characters.
    
    ### TASKS
    Analyze all provided findings.
    For each finding, inspect the code_context carefully and provide:
    'sonar_key': The exact sonar_key value provided in the SONARQUBE DATA. Do NOT invent it.
    'file': The exact filename/path.
    'line': The first line of original_code, not the Sonar line if the fix starts earlier.
    'target_type': The most accurate type of affected code element.
    'target_name': The exact name of the affected target when clearly identifiable.
    'problem': A precise technical risk explanation in MAX 20 words.
    'solution': A precise technical fix instruction in MAX 20 words.
    'original_start_line': The first line of original_code.
    'original_end_line': The last line of original_code.
    'original_code': The COMPLETE code region that must be replaced so the fix is valid.
    'proposed_code': The COMPLETE replacement for original_code.

    ### STRICT REQUIREMENTS FOR 'original_code'
    - It must be the minimal FULL replacement region required for a correct fix.
    - It must include all dependent statements that belong to the same logical block when necessary.
    - It must not be so small that applying proposed_code would break syntax, scope, lifecycle, or behavior.
    - It must not be unnecessarily large if a smaller correct region is sufficient.

    ### STRICT REQUIREMENTS FOR 'proposed_code'
    - It must be a direct replacement for original_code.
    - It must be complete and self-consistent.
    - It must preserve the surrounding contract of the code.
    - It must not rely on omitted hidden lines to remain valid.
    - It must not require additional unstated edits elsewhere unless absolutely unavoidable and clearly implied by the returned replacement itself.

    ### DECISION
    3. 'comment': Write a high-level executive summary in MAX 20 words.
    4. 'decline_pr': Set to true ONLY if there are findings with BLOCKER or CRITICAL severity.

    ### OUTPUT FORMAT
    Return ONLY one strictly valid JSON object matching the provided schema.
    Do not include markdown wrappers.
    Do not include extra text before or after the JSON.

    ### SONARQUBE DATA
    {json.dumps(issues)}
    """

    # Start of intrumentation to measure latency and token usage of the Gemini model
    start_time = time.time()

    # Generate the answer using the Gemini model
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Decision,
            temperature=0,  # Adjust the temperature to cold trying to get the same response
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)),
    )

    # Calculate duration of the response
    duration = time.time() - start_time

    # Execution time to order in Grafana the different executions of the agent
    current_timestamp = time.time()

    # Extract the token usage
    try:
        metric_prompt = int(response.usage_metadata.prompt_token_count)
        metric_response = int(response.usage_metadata.candidates_token_count)
        metric_total_tokens = int(response.usage_metadata.total_token_count)
    except Exception as e:
        metric_prompt, metric_response, metric_total_tokens = 0, 0, 0
        logger.warning(f"Could not retrieve token usage: {e}")

    # Registry and metrics for Prometheus
    registry = CollectorRegistry()
    latency = Gauge(
        "codeguardian_analysis_latency_seconds",
        "Response time of Gemini model (s)",
        registry=registry,
    )

    #
    execution_time = Gauge(
        "codeguardian_last_execution_timestamp",
        "Timestamp of the last agent execution",
        registry=registry,
    )
    execution_time.set(current_timestamp)

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
    total_tokens = Gauge(
        "codeguardian_analysis_total_tokens",
        "Total tokens used in the Gemini model response",
        registry=registry,
    )

    latency.set(duration)
    prompt_tokens.set(metric_prompt)
    response_tokens.set(metric_response)
    total_tokens.set(metric_total_tokens)

    # Send the metrics to Prometheus Pushgateway
    try:
        # To set a tag in grafana
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
            metric_total_tokens,
        )
    except Exception as metric_error:
        logger.error(f"Failed to push metrics to Prometheus Pushgateway: {metric_error}")

    try:
        # Parse the model's response as JSON and validate it against the Decision model
        decision = Decision.model_validate_json(response.text)
        return decision

    except Exception as e:
        logger.error(f"Critical error of Pydantic to parse the JSON: {e}")
        logger.error(f"The response from the model was: {response.text}")
        raise AgentExecutionError("Gemini response parsing failed") from e


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


# If the AI does not provide a valid code replacement, it is better to skip the comment than to publish a broken code suggestion.
def clean_replacement_text(value: str) -> str:
    return value.replace('\\n', '\n').strip('`').strip()


# Check if the AI response contains a valid code replacement that produces a real change in the code
def is_valid_replacement(issue: Issue) -> bool:
    clean_orig = clean_replacement_text(issue.original_code)
    clean_prop = clean_replacement_text(issue.proposed_code)
    return bool(clean_orig and clean_prop and clean_orig != clean_prop)


# Remove extra spaces and blank lines from the code blocks
def normalize_code_block(text: str) -> str:
    if not text:
        return ""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().splitlines()).strip()


# Before posting the comment, validate that the original code block provided by the AI matches exactly with the code in the file for the specified line range
def validate_issue_against_file(issue: Issue, line_tolerance: int = 20) -> bool:
    try:
        lines = _read_file_lines(issue.file)

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


def build_inline_comment_content(issue: Issue) -> str:
    file_extension = issue.file.split(".")[-1] if "." in issue.file else "txt"
    issue_key = build_issue_key(issue)

    clean_orig = clean_replacement_text(issue.original_code)
    clean_prop = clean_replacement_text(issue.proposed_code)

    line_start = int(getattr(issue, "original_start_line", issue.line))
    line_end = int(getattr(issue, "original_end_line", issue.line))

    body = (f"### Code Issue\n\n"
            f"**File:** {issue.file}\n\n"
            f"**Lines:** {line_start}-{line_end}\n\n"
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
            f"{build_hidden_ids([issue_key])}")

    return wrap_agent_comment(body)


# Publish one inline comment per current issue on its target line.
async def post_inline_comment(
    session: ClientSession,
    pr_id: str,
    repo_slug: str,
    issue: Issue,
    workspace: str,
) -> bool:
    try:
        if not is_valid_replacement(issue):
            return False

        if not validate_issue_against_file(issue):
            return False

        line_end = int(getattr(issue, "original_end_line", issue.line))
        content = build_inline_comment_content(issue)

        await session.call_tool(
            name="addPullRequestComment",
            arguments={
                "workspace": workspace,
                "pull_request_id": int(pr_id),
                "repo_slug": repo_slug,
                "content": content,
                "inline": {
                    "path": issue.file,
                    "to": line_end,
                },
            },
        )
        return True
    except Exception as e:
        logger.error(f"Failed to add inline comment: {e}")
        raise


# Read all existing agent comments from the pull request to reconcile current analysis state.
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


# Synchronize PR inline comments with the current analysis state:
# obsolete agent comments are deleted, missing comments are created,
# and no agent comments remain when no issues are detected.
async def synchronize_inline_comments(
    session: ClientSession,
    pr_id: str,
    repo_slug: str,
    workspace: str,
    issues: list[Issue],
) -> int:
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

    logger.info(
        "Issues detected=%s, publishable=%s, skipped_invalid_replacement=%s, skipped_file_mismatch=%s, tracked_agent_comments=%s",
        total_detected,
        len(desired_issues_by_key),
        invalid_replacements,
        invalid_file_matches,
        len(active_inline_comments),
    )

    comments_to_delete: set[int] = set()
    existing_comments_by_issue_key: dict[str, dict] = {}

    # Keep only one comment per issue_key.
    # Duplicates, grouped comments or legacy comments are deleted.
    for comment_id in sorted(active_inline_comments.keys(), reverse=True):
        comment_info = active_inline_comments[comment_id]
        issue_keys = set(comment_info.get("issue_keys", set()))

        if len(issue_keys) != 1:
            comments_to_delete.add(comment_id)
            continue

        issue_key = next(iter(issue_keys))

        if issue_key in existing_comments_by_issue_key:
            comments_to_delete.add(comment_id)
            continue

        existing_comments_by_issue_key[issue_key] = comment_info

    # First, delete clearly obsolete duplicate/grouped comments.
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

    # Delete comments that no longer represent the current desired state
    keys_blocked_by_failed_delete: set[str] = set()

    for issue_key, comment_info in existing_comments_by_issue_key.items():
        comment_id = int(comment_info["comment_id"])

        if comment_id in deleted_comment_ids:
            continue

        if comment_id in failed_comment_ids:
            keys_blocked_by_failed_delete.add(issue_key)
            continue

        inline_data = comment_info.get("inline") or {}
        desired_issue = desired_issues_by_key.get(issue_key)

        should_delete = False

        if desired_issue is None:
            should_delete = True
        elif comment_info.get("resolved", False):
            should_delete = True
        elif comment_info.get("outdated", False):
            should_delete = True
        else:
            expected_path = desired_issue.file
            expected_line = int(getattr(desired_issue, "original_end_line", desired_issue.line))
            current_path = inline_data.get("path")
            current_line = inline_data.get("to")
            current_raw_text = (comment_info.get("raw_text") or "").strip()
            expected_raw_text = build_inline_comment_content(desired_issue).strip()

            if current_path != expected_path or current_line != expected_line:
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
                keys_blocked_by_failed_delete.add(issue_key)

    existing_issue_keys_after_cleanup = {
        issue_key for issue_key, comment_info in existing_comments_by_issue_key.items()
        if int(comment_info["comment_id"]) not in deleted_comment_ids
    }

    created_comments = 0

    for issue_key, issue in desired_issues_by_key.items():
        if issue_key in existing_issue_keys_after_cleanup:
            continue

        if issue_key in keys_blocked_by_failed_delete:
            continue

        created = await post_inline_comment(session, pr_id, repo_slug, issue, workspace)
        if created:
            created_comments += 1
        await asyncio.sleep(0.2)

    logger.info(
        "Comment sync result for PR %s: deleted_comments=%s, blocked_issue_keys=%s, created_comments=%s",
        pr_id,
        len(deleted_comment_ids),
        len(keys_blocked_by_failed_delete),
        created_comments,
    )

    return created_comments


# Turn the final AI decision into visible comments in Bitbucket for a pull request.
async def report_to_bitbucket(pr_id: str, repo_slug: str, workspace: str, decision: Decision) -> None:
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


# Main function to orchestrate the flow of the agent: load the webhook data, fetch and clean SonarQube issues, analyze with Gemini, and report back to Bitbucket.
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

    sanitized_issues = []
    dropped_invalid_issues = 0

    for issue in decision.issues:
        sanitized_issue = sanitize_issue(issue)
        if not is_structurally_valid_issue(sanitized_issue):
            dropped_invalid_issues += 1
            continue
        sanitized_issues.append(sanitized_issue)

    decision.issues = deduplicate_issues(sanitized_issues)

    if dropped_invalid_issues:
        logger.info("Dropped %s structurally invalid issues returned by Gemini", dropped_invalid_issues)

    logger.info(
        "AI analysis completed, reporting the results to Bitbucket. You can also check the detailed metrics of the analysis in Prometheus or Grafana."
    )

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
