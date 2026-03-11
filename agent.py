import argparse
import json
import asyncio
import os
import sys
import google.genai as genai
import logging
import time
from mcp.types import CallToolResult  # Library to manage the results of the MCP tools
from google.genai import (
    types,
)  # Library to manage the configuration and types for the Gemini model API
from mcp import (
    ClientSession,
    StdioServerParameters,
)  # Library to manage the client session with the MCP tools
from mcp.client.stdio import (
    stdio_client,
)  # Library to interact with the MCP tools using standard input/output
from pydantic import BaseModel  # Library for json formating and validation
from prometheus_client import (
    CollectorRegistry,
    Gauge,
    push_to_gateway,
)  # Libraries for Prometheus metrics

# Configure logging to show timestamps and log levels for better debugging and monitoring
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,  # added because avoid the 2> dev/null in Jenkins to show the logs
)
# Set the logging level to WARNING to reduce noise in the logs
logger = logging.getLogger(__name__)
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# -- DATA MODELS --
class Issue(BaseModel):
    file: str
    target_type: str
    target_name: str
    line: int
    problem: str
    severity: str
    solution: str
    proposed_code: str

class Decision(BaseModel):
    decline_pr: bool
    issues: list[Issue]
    comment: str

# -- CLEANING AND EXTRACTING FUNCTIONS --
# Function that loads the json file received from webhook and extract the context
def load_webhook_data(filepath: str) -> tuple[str, str]:
    # Load the JSON file
    with open(filepath, "r") as file:
        data = json.load(file)

    # Extract the pull request ID and project key from the JSON data
    pr_id = data.get("pr_id") or os.getenv(
        "CHANGE_ID"
    )  # Try to get the pr_id from the JSON, if not found, try to get it from the environment variable (for Jenkins compatibility)
    project_key = data.get("project_key")
    project_key = project_key.replace(".git", "").split("/")[-1].lower()

    return pr_id, project_key

# Function that extract the specific code block affected by the issue
def get_code_context(filepath: str, line_number: int, context_window: int = 15) -> str:
    try:
        if not os.path.exists(
            filepath
        ):  # TODO: if I keep this, it is necessary to put the agent.py in all projects
            return "File not found."
        with open(filepath, "r", encoding="utf-8") as file:
            lines = file.readlines()

        # Calculate the start and end lines for the code snippet
        start_line = max(
            0, line_number - context_window - 1
        )  # -1 because line numbers are typically 1-indexed
        end_line = min(len(lines), line_number + context_window)

        snippet = []
        for i in range(start_line, end_line):
            # Here I mark visually the error line
            prefix = ">> " if (i + 1) == line_number else "   "
            snippet.append(f"{prefix}{i + 1}: {lines[i].rstrip()}")

        return "\n".join(snippet)
    except Exception as e:
        return f"Error reading code context: {e}"

# Function to save tokens and clean the json format of the SonarQube results
def clean_sonar_results(raw_results: CallToolResult) -> list[dict]:
    try:
        # Extract the content text from the raw results and parse it as JSON
        content_text = raw_results.content[0].text
        issues_data = json.loads(content_text)

        # If issues_data is a dict with an "issues" key, take the value of that key, otherwise we assume it's already a list of issues
        issues_list = (
            issues_data.get("issues", [])
            if isinstance(issues_data, dict)
            else issues_data
        )

        cleaned = []
        # Verify that the issues are pass as a list, if not, return an empty list
        if isinstance(issues_list, list):
            for issue in issues_list:
                cleaned.append(
                    {
                        "severity": issue.get("severity"),
                        "component": issue.get("component"),
                        "message": issue.get("message"),
                        "line": issue.get("textRange", {}).get(
                            "startLine", 0
                        ),  # Default to 0 if line number is not available
                        "project": issue.get("project"),
                        "file": issue.get("component", "").split(":")[-1],
                    }
                )
        return cleaned
    except Exception as e:
        logger.error(f"Error cleaning SonarQube results: {e}")
        return []

# -- TOOL INTERACTION FUNCTIONS (MCP AND LLM API) --
# Function that search for SonarQube issues using the MCP tool
async def fetch_sonar_issues(project_key: str) -> list[dict]:

    # Configure the SonarQube parameters
    sonar_parameters = StdioServerParameters(
        command="docker",
        args=[
            "run",
            "-i",
            "--rm",
            "--init",
            "--pull=always",
            "--network",
            "services-net",
            "-e",
            f"SONARQUBE_URL=http://sonarqube-server:9000",
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
                        "inNewCodePeriod": "true",  # Necessary bc it analyzes only the code changed in the pull request (or push), no added issues from the other branch
                    },
                )
                if results:
                    logger.info(
                        f"SonarQube analysis completed successfully for project {project_key}."
                    )
                await asyncio.sleep(
                    10
                )  # Wait a moment to ensure the SonarQube container has finished its process and released the resources
    except Exception as e:
        logger.error(f"Failed to connect to SonarQube {e}")
        sys.exit(
            1
        )  # If SonarQube fails, stop the build to avoid false positives in the pull request analysis
    # Clean the SonarQube results to save tokens and optimize the prompt by the most critical issues
    all_issues = clean_sonar_results(results)
    severities = ["BLOCKER", "CRITICAL"]
    cleaned_issues = [issue for issue in all_issues if issue["severity"] in severities]

    if not cleaned_issues:
        cleaned_issues = []

    top_issues = cleaned_issues[:10]  # Limit to the 10 most critical issues

    for issue in top_issues:
        issue["code_context"] = get_code_context(issue["file"], issue["line"])

    return top_issues

# Function that sends the SonarQube issues to the Gemini model for analysis and receives the decision
def analyze_code_with_gemini(project_key: str, issues: list[dict]) -> Decision:
    # Configure the Gemini model parameters
    client = genai.Client(api_key=os.getenv("LLM_AUTH_TOKEN"))
    # Prompt for the model to analyze the SonarQube issues
    prompt = f"""
        Act as a Senior Software Architect and Security Lead. 
        Analyze the following SonarQube technical findings for the project: '{project_key}'.

        ### RULES FOR 'proposed_code':
        - Return ONLY the specific code block that replaces the problematic part.
        - DO NOT include 'import' statements unless they are absolutely new and necessary.
        - DO NOT redefine the entire function if only one or two lines change.
        - Match the indentation and style of the provided 'code_context'.
        - Ensure the code is production-ready and fixes the specific SonarQube finding.
        - MUST use physical line breaks (hard returns) for multiple lines. DO NOT use literal '\n' characters.

        ### TASK:
        1. Identify the 10 most critical issues (if existing) based on severity and technical debt.
        2. For each issue, analyze the 'code_context' snippet to understand the exact code, then provide:
           - 'file': The exact filename/path.
           - 'line': The specific line number.
           - 'target_type': The type of the affected code structure (e.g., "Variable", "Method", "Class", "Interface").
           - 'target_name': The exact name of the affected target (e.g., "conn", "procesar()"). Do NOT invent names.
           - 'problem': A technical explanation of WHY this is a risk based on reading the actual code snippet.
           - 'solution': Clear instruction on how to fix it. Use actual variable names from the snippet. Do NOT invent code.
           - 'proposed_code': The clean code snippet fixing the issue. Keep it concise.
        3. 'comment': A 2-sentence high-level executive summary for the lead developer.
        4. 'decline_pr': Set to 'true' ONLY if there are findings with 'BLOCKER' or 'CRITICAL' severity.

        ### OUTPUT FORMAT:
        Return ONLY a strictly valid JSON object that matches the provided schema. 
        Do not include markdown wrappers like ```json.

        ### SONARQUBE DATA:
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
        pr_id = os.getenv("CHANGE_ID")
        build_id = os.getenv("BUILD_NUMBER", "local_build")
        if pr_id:
            event_type = "pull_request"
            display_label = f"{project_key}-PR-{pr_id}"
        else:
            event_type = "push"
            display_label = f"{project_key}-Push-{build_id}"

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
        logger.error(
            f"Failed to push metrics to Prometheus Pushgateway: {metric_error}"
        )

    try:
        # Parse the model's response as JSON and validate it against the Decision model
        decision = Decision.model_validate_json(response.text)
        return decision

    except Exception as e:
        logger.error(f"Critical error of Pydantic to parse the JSON: {e}")
        logger.error(f"The response from the model was: {response.text}")
        sys.exit(1)  # If the AI fails, stop the build

# Function to post comment on Bitbucketusing the MCP tool
async def post_comment(
    session: ClientSession,
    pr_id: str,
    project_key: str,
    comment: list[str],
    workspace: str,
) -> None:
    for text_chunk in comment:
        try:
            await session.call_tool(
                name="addPullRequestComment",
                arguments={
                    "workspace": workspace,
                    "pull_request_id": int(pr_id),
                    "repo_slug": project_key,
                    "content": text_chunk,
                },
            )
            logger.info(f"Comment added successfully to the pull request {pr_id}")
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Failed to add comment: {e}")

# Function to create a draft PR (using to report in case of push events)
async def create_draft_pr(session: ClientSession, project_key: str, workspace: str, source_branch: str) -> str | None:
    try:
        logger.info(f"Creating draft pull request for project {project_key} in branch {source_branch} to report the analysis results.")
        results= await session.call_tool(
            name="createDraftPullRequest",
            arguments={
                "workspace": workspace,
                "repo_slug": project_key,
                "title": f"Draft: Analysis Report {project_key} : {source_branch}",
                "description": "Automatic report generated by CodeGuardian after push.",
                "sourceBranch": source_branch, 
                "targetBranch": os.getenv("DEFAULT_BRANCH", "main"),      
            },
        )
        pr_data = json.loads(results.content[0].text)
        new_pr_id = pr_data.get("id")
        
        logger.info(f"Draft pull request created successfully for project {project_key}")
        # Return the new PR ID if created successfully, otherwise return None
        return str(new_pr_id) if new_pr_id is not None else None
    except Exception as e:
        logger.error(f"Failed to create draft pull request: {e}")
        return None

# Function to publish the draft PR on Bitbucket using the MCP tool (in case we want to notify the developer with the draft PR and let them decide)
async def publish_draft_pr(session: ClientSession, pr_id: str, project_key: str, workspace: str) -> None:
    try:
        await session.call_tool(
            name="publishDraftPullRequest",
            arguments={
                "workspace": workspace,
                "pull_request_id": str(pr_id),
                "repo_slug": project_key,
            },
        )
        logger.info(f"Draft pull request {pr_id} published successfully.")
    except Exception as e:
        logger.error(f"Failed to publish draft pull request: {e}")

# Function to decline the pull request on Bitbucket using the MCP tool
async def decline_pull_request(
    session: ClientSession, pr_id: str, project_key: str, workspace: str
) -> None:
    try:
        await session.call_tool(
            name="declinePullRequest",
            arguments={
                "workspace": workspace,
                "pull_request_id": int(pr_id),
                "repo_slug": project_key,
                "message": "Pull request declined by CodeGuardian. Found CRITICAL/BLOCKER issues.",
            },
        )
        logger.info(f"Pull request {pr_id} declined successfully.")
    except Exception as e:
        logger.error(f"Failed to decline PR: {e}")

# Function to post inline comments on specfic lines of the pull request on BitBucket using MCP tool
async def post_inline_comment(
    session: ClientSession, pr_id: str, project_key: str, issue: Issue, workspace: str
) -> None:
    try:
        # Extract the file extension to color in bitbucket
        file_extension = issue.file.split(".")[-1] if "." in issue.file else "txt"
        content = (
            f"**File:** `{issue.file}`\n\n"
            f"**Type:** `{issue.target_type}()` | **Name:** `{issue.target_name}`\n\n"
            f"**Line:** `{issue.line}`\n\n"
            f"**Problem (`{issue.severity}`):** {issue.problem}\n\n"
            f"**Proposed solution:** {issue.solution}\n\n"
            f"**Proposed code:**\n\n"
            f"```{file_extension}\n"
            f"{issue.proposed_code.replace('\\n', '\n')}\n"
            f"```"
        )
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
    except Exception as e:
        logger.error(f"Failed to add inline comment: {e}")

# Function to approve the pull request on Bitbucket using the MCP tool
async def approve_pull_request(
    session: ClientSession, pr_id: str, project_key: str, workspace: str
) -> None:
    try:
        await session.call_tool(
            name="approvePullRequest",
            arguments={
                "workspace": workspace,
                "pull_request_id": int(pr_id),
                "repo_slug": project_key,
                "message": "Pull request approved by CodeGuardian. No critical issues found.",
            },
        )
        logger.info(f"Pull request {pr_id} approved successfully.")
    except Exception as e:
        logger.error(f"Failed to approve PR: {e}")

# Function to report the analysis results to Bitbucket
async def report_to_bitbucket(pr_id: str, project_key: str, decision: Decision) -> None:
    # Configure the Bitbucket tool parameters
    bitbucket_env = os.environ.copy()  # Need this bc need to inherit the PATH
    bitbucket_env.update(
        {
            "BITBUCKET_URL": os.getenv(
                "BITBUCKET_URL", "https://api.bitbucket.org/2.0"
            ),
            "BITBUCKET_WORKSPACE": os.getenv("BITBUCKET_WORKSPACE", "medinafdzz"),
            "BITBUCKET_USERNAME": os.getenv("BITBUCKET_USERNAME"),
            "BITBUCKET_PASSWORD": os.getenv("BITBUCKET_APP_TOKEN"),
        }
    )

    workspace = os.getenv("BITBUCKET_WORKSPACE", "medinafdzz")
    decline = decision.decline_pr
    # Flag to detect if the build should stop (I put this to avoid all the tracebacks)
    should_exit = False

    try:
        # Start the MCP client session for Bitbucket
        async with stdio_client(
            StdioServerParameters(
                command="npx",
                args=["-y", "--quiet", "bitbucket-mcp@latest"],
                env=bitbucket_env,
            )
        ) as (read, write):
            async with ClientSession(read, write) as session_bb:
                await session_bb.initialize()

                # Check if there is a pr_id and is valid
                is_pr = pr_id and str(pr_id).lower() != "null"

                # Manage the pull request and push cases based on the model's decision
                # CASE 1: PULL REQUEST EVENT
                if is_pr:
                    logger.info(f"\n--- PULL REQUEST DETECTED - {pr_id} ---")
                    summary_comment = [
                        f"**CodeGuardian Analysis Summary**\n\n{decision.comment}\n\n"
                    ]  # between [] bc post_commend need a list of strings
                    await post_comment(
                        session_bb, pr_id, project_key, summary_comment, workspace
                    )

                    # Sugestion inline comments for each issue
                    if decision.issues:
                        logger.info(f"\n--- INLINE COMMENTS FOR THE MOST CRITICAL ISSUES ---")
                        for issue in decision.issues:
                            await post_inline_comment(
                                session_bb, pr_id, project_key, issue, workspace
                            )
                            await asyncio.sleep(
                                1
                            )  # Sleep to avoid hitting rate limits when posting multiple inline comments
                    # CASE 1.1: If the AI detects critical issues, decline the PR and stop the build
                    if decline:
                        await decline_pull_request(session_bb, pr_id, project_key, workspace)
                        should_exit = True  # Set the flag to stop the build after reporting to Bitbucket
                    # CASE 1.2: LOW/MEDIUM issues, approve by the agent
                    else:
                        # Approve no merge, bc the merge is function of the developer
                        await approve_pull_request(session_bb, pr_id, project_key, workspace)
                        logger.info("Build approved based on the analysis results. No critical issues found.")
                # CASE 2: PUSH EVENT
                else:
                    logger.info("\n--- PUSH EVENT ---")
                    current_branch = os.getenv("GIT_BRANCH") or os.getenv("BRANCH_NAME") or "unknown"

                    # Normalize branch names from Jenkins formats
                    if current_branch.startswith("origin/"):
                        current_branch = current_branch.split("/", 1)[1]
                    if current_branch.startswith("refs/heads/"):
                        current_branch = current_branch.replace("refs/heads/", "", 1)

                    target_branch = os.getenv("DEFAULT_BRANCH", "main")

                    logger.info(
                        f"CodeGuardian analysis summary for the push in project '{project_key}':\n\n{decision.comment}\n\n"
                    )

                    if decision.issues:
                        for index, issue in enumerate(decision.issues):
                            logger.info(
                                f"**- Problem {index+1} ({issue.severity}) ({issue.file} :{issue.line}):** {issue.problem}"
                            )
                            logger.info(f"  **Proposed fix:** {issue.solution}\n")

                    # CASE 2.1: CRITICAL ISSUES DETECTED - Create a Draft PR to notify the developer and stop the build
                    if decline and current_branch not in ["main", "master", "unknown"] and current_branch != target_branch:
                        logger.warning(
                            f"Critical issues detected. Opening Draft PR for branch '{current_branch}' -> '{target_branch}'."
                        )

                        draft_pr_id = await create_draft_pr(session_bb, project_key, workspace, current_branch)

                        if draft_pr_id:
                            summary = [f"**Analysis Summary for {current_branch}**\n\n{decision.comment}\n\n"]
                            await post_comment(session_bb, draft_pr_id, project_key, summary, workspace)

                            for issue in decision.issues:
                                await post_inline_comment(session_bb, draft_pr_id, project_key, issue, workspace)
                                await asyncio.sleep(1)

                            await publish_draft_pr(session_bb, draft_pr_id, project_key, workspace)
                            logger.info(f"Draft PR {draft_pr_id} published for developer review.")
                            should_exit = True
                        else:
                            logger.error("Failed to create draft pull request to report push analysis results.")
                            should_exit = True

                    # CASE 2.2: Critical issues in default branch
                    elif decline:
                        logger.error(
                            "Critical issues detected by the AI agent in push event. Stopping the build."
                        )
                        should_exit = True

                    # CASE 2.3: No critical issues
                    else:
                        logger.info("No critical issues detected by the AI agent in the push event. Build approved.")
    except Exception as e:
        logger.error(f"Failed to connect to Bitbucket {e}")
        sys.exit(1)

    if should_exit:
        logger.error("Build stopped due to critical issues detected by the AI agent.")
        sys.exit(1)

# -- PRINCIPAL FUNCTION TO ORCHESTRATE THE STEPS --
async def main() -> None:
    # Parse the command-line arguments to get the path to the JSON file
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--file", required=True, help="Path to the JSON file to analyze"
    )
    args = parser.parse_args()

    # First step: load the context
    pr_id, project_key = load_webhook_data(args.file)
    if not project_key:
        logger.error(
            "Project key not found in the JSON file. Please provide a valid project key."
        )
        sys.exit(1)

    # Extract and clean the SonarQube data
    issues = await fetch_sonar_issues(project_key)

    if not issues:
        logger.info(
            "No critical issues found by SonarQube. Approving the pull request without AI analysis."
        )

        auto_decision = Decision(
            decline_pr=False,
            issues=[],
            comment="CodeGuardian has automatically approved this Pull Request. SonarQube did not detect any CRITICAL or BLOCKER issues in the modified code.",
        )

        await report_to_bitbucket(pr_id, project_key, auto_decision)
        return

    logger.info(
        f"CRITICAL or BLOCKER issues found by SonarQube: {len(issues)}. Proceeding with AI analysis."
    )
    # Analyze the code with the AI
    decision = analyze_code_with_gemini(project_key, issues)

    logger.info(
        "AI analysis completed, reporting the results to Bitbucket. You can also check the detailed metrics of the analysis in Prometheus or Grafana."
    )

    # Report and act in SOnarQube based on the AI decision
    await report_to_bitbucket(pr_id, project_key, decision)

if __name__ == "__main__":
    asyncio.run(main())
