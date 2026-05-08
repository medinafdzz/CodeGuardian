import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from codeguardian.logging_utils import logger
from codeguardian.models import AgentExecutionError
from codeguardian.text import get_code_context, resolve_scope


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

    filtered_issues = [
        issue for issue in all_issues
        if issue.get("severity") in severity_order and os.path.exists(issue.get("file", ""))
    ]

    filtered_issues.sort(key=lambda issue: (
        severity_order.get(issue.get("severity"), 99),
        issue.get("file", ""),
        issue.get("line", 0),
    ))

    max_issues = int(os.getenv("CODEGUARDIAN_MAX_ISSUES", "35"))
    top_issues = filtered_issues[:max_issues]  # Limit the number of issues sent to the AI after sorting by severity

    for issue in top_issues:
        issue["code_context"] = get_code_context(issue["file"], issue["line"])

        scope = resolve_scope(issue["file"], issue["line"])
        issue["scope_kind"] = scope.kind
        issue["scope_name"] = scope.name
        issue["scope_start_line"] = scope.start_line
        issue["scope_end_line"] = scope.end_line

    return top_issues
