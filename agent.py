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
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,  # added because avoid the 2> dev/null in Jenkins to show the logs
)
# Set the logging level to WARNING to reduce noise in the logs
logger = logging.getLogger(__name__)
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


# These models are the data the agent moves around between Sonar, Gemini and Bitbucket.class
class Issue(BaseModel):
    sonar_key: str
    file: str
    target_type: str
    target_name: str
    line: int
    problem: str
    severity: str
    solution: str
    original_code: str
    proposed_code: str

class Decision(BaseModel):
    decline_pr: bool
    issues: list[Issue]
    comment: str

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

# Read the key of the issues that just have been commented
def extract_issue_key(comment_text: str) -> list[str]:
    keys = set()

    blocks = re.findall(r"<!--\s*CodeGuardian-IDs:\s*([^>]+?)\s*-->", comment_text)
    for block in blocks:
        for key in block.split(","):
            key = key.strip()
            if key:
                keys.add(key)

    return list(keys)

# Get the project key and pull request ID from the webhook input and leave them in a format the rest of the flow can reuse.
def load_webhook_data(filepath: str) -> tuple[str, str]:
    with open(filepath, "r") as file:
        data = json.load(file)

    project_key = data.get("project_key")
    pr_id = data.get("pr_id")

    if not project_key:
        logger.error("Project key not found in the JSON file.")
        return "", ""

    if not pr_id:
        logger.error("Pull request ID not found in the JSON file.")
        return "", ""

    return project_key.replace(".git", "").split("/")[-1].lower(), str(pr_id)

# Add nearby lines around the issue so the AI can understand the problem with some real context.
@lru_cache(maxsize=256)
def _read_file_lines(filepath: str) -> list[str]:
    if not os.path.exists(filepath):
        raise FileNotFoundError("File not found.")
    with open(filepath, "r", encoding="utf-8") as file:
        return file.readlines()

def get_code_context(filepath: str, line_number: int, context_window: int = 25) -> str:
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
    try:
        # Extract the content text from the raw results and parse it as JSON
        content_text = raw_results.content[0].text
        issues_data = json.loads(content_text)

        # If issues_data is a dict with an "issues" key, take the value of that key, otherwise we assume it's already a list of issues
        issues_list = issues_data.get("issues", []) if isinstance(issues_data, dict) else issues_data

        cleaned = []
        # Verify that the issues are pass as a list, if not, return an empty list
        if isinstance(issues_list, list):
            for issue in issues_list:
                cleaned.append({
                    "sonar_key": issue.get("key", "NO_KEY"),
                    "severity": issue.get("severity"),
                    "component": issue.get("component"),
                    "message": issue.get("message"),
                    "line": issue.get("textRange", {}).get("startLine",
                                                           0),  # Default to 0 if line number is not available
                    "project": issue.get("project"),
                    "file": issue.get("component", "").split(":")[-1],
                })
        return cleaned
    except Exception as e:
        logger.error(f"Error cleaning SonarQube results: {e}")
        return []

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
                        "inNewCodePeriod": "true",  # Necessary bc it analyzes only the code changed in the pull request, no added issues from the other branch
                    },
                )
                if results:
                    logger.info(f"SonarQube analysis completed successfully for project {project_key}.")
    except Exception as e:
        logger.error(f"Failed to connect to SonarQube {e}")
        sys.exit(1)  # If SonarQube fails, stop the build to avoid false positives in the pull request analysis
    # Clean the SonarQube results to save tokens and optimize the prompt by the most critical issues
    all_issues = clean_sonar_results(results)
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

    cleaned_issues = [
        issue for issue in all_issues
        if issue.get("severity") in severity_order
    ]

    cleaned_issues.sort(
        key=lambda issue: (
            severity_order.get(issue.get("severity"), 99),
            issue.get("file", ""),
            issue.get("line", 0),
        )
    )

    max_issues = int(os.getenv("CODEGUARDIAN_MAX_ISSUES", "20"))
    top_issues = cleaned_issues[:max_issues]  # Limit the number of issues sent to the AI after sorting by severity

    for issue in top_issues:
        issue["code_context"] = get_code_context(issue["file"], issue["line"], context_window=25)

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

    ### RULES FOR 'proposed_code'
    - Return ONLY the exact raw code snippet that replaces the problematic region.
    - DO NOT wrap the code in markdown blocks.
    - DO NOT include explanations inside the code.
    - Preserve the original programming language, coding style, naming style, formatting style, and indentation style of the provided code context.
    - Prefer the smallest safe change, but NEVER sacrifice correctness for brevity.
    - The replacement must be syntactically valid in the original language.
    - The replacement must remain semantically coherent with the surrounding code context.
    - DO NOT invent APIs, variables, functions, classes, modules, imports, types, constants, macros, or symbols that are not already present or clearly required.
    - Only add imports, includes, using statements, dependencies, declarations, or boilerplate when they are strictly necessary for correctness.
    - NEVER return a partial fragment if a larger replacement region is required for correctness.
    - Preserve variable lifetime, visibility, ownership, mutability, initialization order, and valid scope.
    - Preserve resource lifecycle semantics, cleanup semantics, and error-handling semantics when relevant.
    - Preserve control flow correctness: do not leave unresolved branches, missing returns, broken loops, orphaned conditions, unreachable code, or unbalanced delimiters.
    - Do not introduce unresolved identifiers, dangling references, invalid state transitions, or invalid object/resource usage.
    - Do not move declarations into a narrower scope if they are used later.
    - Do not move statements outside the scope they depend on.
    - Do not change behavior unrelated to the finding unless strictly necessary to produce a valid fix.
    - When several nearby findings in the same file are solved by the exact same replacement region, generate the same replacement block for them.
    - When a correct fix requires replacing a larger logical block, return that larger block instead of a smaller unsafe fragment.
    - The final replacement must be production-ready.
    - Assume the replacement will be directly applied over the original_code block, so it must compile, run, or parse correctly in that location.
    - For interpreted languages, ensure the replacement is executable and structurally valid.
    - For compiled languages, ensure the replacement is compilable in context.
    - For memory/resource/concurrency issues, ensure the fix does not introduce leaks, deadlocks, race conditions, invalid ownership, double release, or scope misuse.
    - Do not return placeholder text such as TODO, FIXME, pseudocode, ellipsis, or comments like "rest of code unchanged".
    - MUST use physical line breaks for multiline code. DO NOT use literal '\\n' characters.

    ### TASK
    1. Analyze all provided findings.
    2. For each finding, inspect the code_context carefully and provide:
    - 'sonar_key': The exact sonar_key value provided in the SONARQUBE DATA. Do NOT invent it.
    - 'file': The exact filename/path.
    - 'line': The specific line number.
    - 'target_type': The most accurate type of affected code element (for example: Variable, Function, Method, Class, Module, Statement, Block, Query, Resource, Loop, Condition, Thread, Object, Memory Buffer, File Handle).
    - 'target_name': The exact name of the affected target when clearly identifiable from the code. Do NOT invent names. If no clear name exists, use the smallest precise existing identifier or construct name from the snippet.
    - 'problem': A precise technical risk explanation in MAX 20 words.
    - 'solution': A precise technical fix instruction in MAX 20 words.
    - 'original_code': The COMPLETE code region that must be replaced so the fix is valid.
    - 'proposed_code': The COMPLETE replacement for original_code.

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
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
        ),
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

    logging.info("Sending metrics to Prometheus Pushgateway")

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
            f"Metrics pushed to Prometheus Pushgateway: event_type={event_type}, build_number={build_id}, latency={duration:.2f}s, prompt_tokens={metric_prompt}, response_tokens={metric_response}, total_tokens={metric_total_tokens}"
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
        sys.exit(1)  # If the AI fails, stop the build

# Leave the general summary in the PR so the developer can see the main result quickly.
async def post_comment(session: ClientSession, pr_id: str, project_key: str, comment: str, workspace: str) -> None:
    try:
        await session.call_tool(
            name="addPullRequestComment",
            arguments={
                "workspace": workspace,
                "pull_request_id": int(pr_id),
                "repo_slug": project_key,
                "content": comment,
            },
        )
        logger.info(f"Comment added successfully to the pull request {pr_id}")
    except Exception as e:
        logger.error(f"Failed to add comment: {e}")
        raise

# Add each comment to its exact line so the developer does not have to search for it by hand.
async def post_inline_comment(session: ClientSession, pr_id: str, project_key: str, issue: Issue,
                              workspace: str) -> bool:
    try:
        # Extract the file extension to color in bitbucket
        file_extension = issue.file.split(".")[-1] if "." in issue.file else "txt"
        issue_key = build_issue_key(issue)

       # Clean both blocks to avoid markdown issues
        clean_orig = issue.original_code.replace('\\n', '\n').strip('`').strip()
        clean_prop = issue.proposed_code.replace('\\n', '\n').strip('`').strip()

        if not clean_orig or not clean_prop or clean_orig == clean_prop:
            logger.info(f"Skipping issue {issue_key} because it does not produce a real code change.")
            return False

        line_start = int(issue.line)
        original_line_count = max(1, len(clean_orig.splitlines()))
        line_end = line_start + original_line_count - 1

        content = (f"### Code Issue\n\n"
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
                f"<!-- CodeGuardian-IDs: {issue_key} -->")

        await session.call_tool(
            name="addPullRequestComment",
            arguments={
                "workspace": workspace,
                "pull_request_id": int(pr_id),
                "repo_slug": project_key,
                "content": content,
                "inline": {
                    "path": issue.file,
                    "to": int(issue.line),
                },
            },
        )
        logger.info(f"Inline comment added successfully to the pull request {pr_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to add inline comment: {e}")
        raise

# Request all comments from the pull request so can inspect previous feedback
async def get_inline_comments(session: ClientSession, pr_id: str, project_key: str, workspace: str) -> dict[str, dict]:
    try:
        results = await session.call_tool(
            name="getPullRequestComments",
            arguments={
                "workspace": workspace,
                "pull_request_id": int(pr_id),
                "repo_slug": project_key,
                "all": True,  # Get all comments, including inline ones
            },
        )

        comments_data = json.loads(results.content[0].text)

        if isinstance(comments_data, dict):
            comments = comments_data.get("values", [])
        elif isinstance(comments_data, list):
            comments = comments_data
        else:
            comments = []

        logger.info(f"DEBUG: Bitbucket returned {len(comments)} comments in total for this PR.")
        active_inline_comments = {}

        # Review every comment in the PR to find the ones created by the agent
        for comment in comments:

            if comment.get("deleted", False):
                continue

            if comment.get("resolved", False):
                continue
            
            if comment.get("parent"):
                continue

            raw_text = comment.get("content", {}).get("raw", "")
            issue_keys = extract_issue_key(raw_text)

            if issue_keys:
                # Extract the main metadata
                comment_id = int(comment.get("id"))
                resolved = comment.get("resolved", False)
                inline_data = comment.get("inline")

                # Track all agent comments with IDs
                for issue_key in issue_keys:
                    active_inline_comments[issue_key] = {
                        "comment_id": comment_id,
                        "resolved": resolved,
                        "inline": inline_data,
                    }

            logger.info(f"RAW COMMENT JSON: {json.dumps(comment)}")
        return active_inline_comments

    except Exception as e:
        logger.error(f"Failed to retrieve inline comments: {e}")
        raise

# If an issue was solve it must be indicated by marking the comment as resolved in Bitbucket
async def resolve_inline_comment(session: ClientSession, pr_id: str, project_key: str, comment_id: str,
                                 workspace: str) -> None:
    try:
        await session.call_tool(
            name="resolveComment",
            arguments={
                "workspace": workspace,
                "pull_request_id": int(pr_id),
                "repo_slug": project_key,
                "comment_id": int(comment_id),
            },
        )
        logger.info(f"Comment {comment_id} resolved successfully in pull request {pr_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to resolve comment {comment_id}: {e}")
        return False

# When the agent mark the resolved issue, should desapear from the PR, and when a new issue appears in the analysis, should be added as a new comment.
async def synchronize_inline_comments(session: ClientSession, pr_id: str, project_key: str, workspace: str,
                                      issues: list[Issue]) -> int:
    active_inline_comments = await get_inline_comments(session, pr_id, project_key, workspace)

    current_issues_by_key = {build_issue_key(issue): issue for issue in issues}
    current_issue_keys = set(current_issues_by_key.keys())
    active_issue_keys = set(active_inline_comments.keys())

    # Group the tracked issue keys by comment_id to avoid resolving a grouped comment too early
    comment_to_issue_keys = {}
    resolved_ids = set()
    forced_republish_keys = set()

    for issue_key, comment_info in active_inline_comments.items():
        comment_id = comment_info["comment_id"]
        if comment_id not in comment_to_issue_keys:
            comment_to_issue_keys[comment_id] = set()
        comment_to_issue_keys[comment_id].add(issue_key)

    # Resolve or refresh comments depending on how their tracked issue keys evolved
    for comment_id, comment_issue_keys in comment_to_issue_keys.items():
        if comment_id in resolved_ids:
            continue

        active_keys_for_comment = comment_issue_keys.intersection(current_issue_keys)

        # Case 1: all keys disappeared -> resolve old comment
        if not active_keys_for_comment:
            sample_issue_key = next(iter(comment_issue_keys))
            comment_info = active_inline_comments.get(sample_issue_key)

            if not comment_info or comment_info.get("resolved", False):
                continue

            logger.info(
                f"Resolving stale comment_id={comment_id} "
                f"old_keys={list(comment_issue_keys)} "
                f"active_keys=[]"
            )

            resolved = await resolve_inline_comment(session, pr_id, project_key, comment_id, workspace)
            if resolved:
                resolved_ids.add(comment_id)
                await asyncio.sleep(0.2)
            continue

        # Case 2: mixed state (some keys resolved, some still active) -> resolve and republish active subset
        if active_keys_for_comment != comment_issue_keys:
            sample_issue_key = next(iter(comment_issue_keys))
            comment_info = active_inline_comments.get(sample_issue_key)

            logger.info(
                f"Refreshing mixed comment_id={comment_id} "
                f"old_keys={list(comment_issue_keys)} "
                f"still_active={list(active_keys_for_comment)}"
            )

            if comment_info and not comment_info.get("resolved", False):
                resolved = await resolve_inline_comment(session, pr_id, project_key, comment_id, workspace)
                if resolved:
                    resolved_ids.add(comment_id)
                    await asyncio.sleep(0.2)

            forced_republish_keys.update(active_keys_for_comment)

    # Create comments for truly new issues plus active keys from mixed comments we just refreshed
    new_issue_keys = (current_issue_keys - active_issue_keys).union(forced_republish_keys)
    grouped_candidates = {}

    for issue_key in new_issue_keys:
        issue = current_issues_by_key[issue_key]

        clean_original = issue.original_code.replace('\\n', '\n').strip('`').strip()
        clean_proposed = issue.proposed_code.replace('\\n', '\n').strip('`').strip()

        group_key = (issue.file, clean_original, clean_proposed)

        if group_key not in grouped_candidates:
            grouped_candidates[group_key] = []

        grouped_candidates[group_key].append(issue)

    grouped_new_issues = []

    for (file_path, clean_original, clean_proposed), issues_in_group in grouped_candidates.items():
        issues_in_group.sort(key=lambda x: x.line)

        current_cluster = [issues_in_group[0]]

        for issue in issues_in_group[1:]:
            if issue.line - current_cluster[-1].line <= 3:
                current_cluster.append(issue)
            else:
                grouped_new_issues.append((file_path, clean_original, clean_proposed, current_cluster))
                current_cluster = [issue]

        grouped_new_issues.append((file_path, clean_original, clean_proposed, current_cluster))

    created_comments = 0

    #Publication of the comments

    for file_path, clean_original, clean_proposed, issue_group in grouped_new_issues:

        issue_group.sort(key=lambda x: x.line)
        min_line = issue_group[0].line
        max_line = issue_group[-1].line
        base_issue = issue_group[0]

        if len(issue_group) == 1:
            created = await post_inline_comment(session, pr_id, project_key, base_issue, workspace)
            if created:
                created_comments += 1
            await asyncio.sleep(0.2)
            continue

        if not clean_original or not clean_proposed or clean_original == clean_proposed:
            logger.info(
                f"Skipping grouped comment in {file_path} lines {min_line}-{max_line} because it does not produce a real code change."
            )
            continue

        file_extension = file_path.split(".")[-1] if "." in file_path else "txt"

        combined_problems = "\n".join(
            [f"- Line {i.line} ({i.severity}): {i.problem}" for i in issue_group]
        )
        group_issue_keys = list(dict.fromkeys(build_issue_key(i) for i in issue_group))
        hidden_ids = f"<!-- CodeGuardian-IDs: {','.join(group_issue_keys)} -->"

        content = (f"### Block Refactor (Lines {min_line} - {max_line})\n\n"
                f"**File:** {file_path}\n\n"
                f"**Lines:** {min_line}-{max_line}\n\n"
                f"**Issues Detected in this block:**\n\n"
                f"{combined_problems}\n\n"
                f"**Solution:** {base_issue.solution}\n\n"
                f"**Block to substitute:**\n"
                f"```{file_extension}\n"
                f"{clean_original}\n"
                f"```\n\n"
                f"**Refactored Code:**\n"
                f"```{file_extension}\n"
                f"{clean_proposed}\n"
                f"```\n\n"
                f"{hidden_ids}")

        try:
            await session.call_tool(
                name="addPullRequestComment",
                arguments={
                    "workspace": workspace,
                    "pull_request_id": int(pr_id),
                    "repo_slug": project_key,
                    "content": content,
                    "inline": {
                        "path": file_path,
                        "to": int(min_line),
                    },
                },
            )
            logger.info(
                f"Grouped inline comment covering lines {min_line}-{max_line} added successfully to PR {pr_id}"
            )
            created_comments += 1
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.error(f"Failed to add grouped inline comment: {e}")
            raise

    return created_comments

# Turn the final AI decision into visible comments in Bitbucket for a pull request.
async def report_to_bitbucket(pr_id: str, project_key: str, decision: Decision) -> None:
    # Configure the Bitbucket tool parameters
    bitbucket_env = os.environ.copy()  # Need this bc need to inherit the PATH
    bitbucket_env.update({
        "BITBUCKET_URL": os.getenv("BITBUCKET_URL", "https://api.bitbucket.org/2.0"),
        "BITBUCKET_WORKSPACE": os.getenv("BITBUCKET_WORKSPACE", "medinafdzz"),
        "BITBUCKET_USERNAME": os.getenv("BITBUCKET_USERNAME"),
        "BITBUCKET_PASSWORD": os.getenv("BITBUCKET_APP_TOKEN"),
    })

    workspace = os.getenv("BITBUCKET_WORKSPACE", "medinafdzz")
    decline = decision.decline_pr

    if not pr_id or str(pr_id).lower() == "null":
        logger.error("No valid pull request ID was provided.")
        sys.exit(1)

    try:
        # Start the MCP client session for Bitbucket
        async with stdio_client(
                StdioServerParameters(
                    command="bitbucket-mcp",
                    args=[],
                    env=bitbucket_env,
                )) as (read, write):
            async with ClientSession(read, write) as session_bb:
                await session_bb.initialize()
                # Publish the general summary comment in the PR activity
                if decline:
                    summary_comment = (
                        f"**CodeGuardian Analysis Summary**\n\n"
                        f"Critical issues have been detected in this pull request and should be reviewed before merging.\n\n"
                        f"{decision.comment}\n\n"
                    )
                elif decision.issues:
                    summary_comment = (
                        f"**CodeGuardian Analysis Summary**\n\n"
                        f"Issues have been detected in this pull request, but none of them require rejecting the PR.\n\n"
                        f"{decision.comment}\n\n"
                    )
                else:
                    summary_comment = (
                        f"**CodeGuardian Analysis Summary**\n\n"
                        f"No relevant issues have been detected in this pull request.\n\n"
                        f"{decision.comment}\n\n"
                    )

                await post_comment(session_bb, pr_id, project_key, summary_comment, workspace)

                #Synchronize the inline comments with the issues detected by the AI
                created_inline_comments = await synchronize_inline_comments(session_bb, pr_id, project_key, workspace,
                                                                            decision.issues)

                issue_count = len(decision.issues)

                logger.info(f"Analysis results posted to PR {pr_id}: "
                            f"{issue_count} active issues in current analysis, "
                            f"{created_inline_comments} new inline comments created.")

    except Exception as e:
        logger.error(f"Failed to report analysis results to Bitbucket: {e}")
        sys.exit(1)

# Main function to orchestrate the flow of the agent: load the webhook data, fetch and clean SonarQube issues, analyze with Gemini, and report back to Bitbucket.
async def main() -> None:
    # Parse the command-line arguments to get the path to the JSON file
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Path to the JSON file to analyze")
    args = parser.parse_args()

    # First step: load the context
    project_key, pr_id = load_webhook_data(args.file)
    if not project_key or not pr_id:
        logger.error("Project key or pull request ID not found in the JSON file. Please provide valid webhook data.")
        sys.exit(1)

    # Extract and clean the SonarQube data
    issues = await fetch_sonar_issues(project_key)

    if not issues:
        logger.info("No relevant issues found by SonarQube. Posting a clean analysis summary to the pull request.")

        auto_decision = Decision(
            decline_pr=False,
            issues=[],
            comment=
            "CodeGuardian analyzed this pull request and did not detect any relevant issues in the modified code.",
        )

        await report_to_bitbucket(pr_id, project_key, auto_decision)
        return

    logger.info(f"Relevant issues found by SonarQube: {len(issues)}. Proceeding with AI analysis.")
    # Analyze the code with the AI
    decision = analyze_code_with_gemini(project_key, issues)
    decision.issues = deduplicate_issues(decision.issues)

    logger.info(
        "AI analysis completed, reporting the results to Bitbucket. You can also check the detailed metrics of the analysis in Prometheus or Grafana."
    )

    # Report and act in SOnarQube based on the AI decision
    await report_to_bitbucket(pr_id, project_key, decision)

if __name__ == "__main__":
    asyncio.run(main())
