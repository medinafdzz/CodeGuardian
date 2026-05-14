import os
from typing import Any, Literal, cast

from mcp.server.fastmcp import FastMCP

from codeguardian import mcp_tools


McpTransport = Literal["stdio", "sse", "streamable-http"]


def _server_port() -> int:
    try:
        return int(os.getenv("CODEGUARDIAN_MCP_PORT", "8010"))
    except ValueError:
        return 8010


mcp = FastMCP(
    "CodeGuardian",
    instructions=(
        "CodeGuardian is the source of truth for already-generated PR suggestions. "
        "Do not perform a manual repository review when the user asks for CodeGuardian output. "
        "Mandatory trigger mapping: if the user writes 'code review', call the code_review tool; "
        "if the user writes 'code improvement' or 'code improvements', call the code_improvement "
        "or code_improvements tool. These tools must show numbered CodeGuardian suggestions with "
        "File, Line, Original code, Explanation and Proposed code. After the developer chooses "
        "1, 2, 3, combinations, or all, call the matching apply tool. Replacement application is "
        "local-workspace only."
    ),
    host=os.getenv("CODEGUARDIAN_MCP_HOST", "0.0.0.0"),
    port=_server_port(),
    streamable_http_path=os.getenv("CODEGUARDIAN_MCP_PATH", "/mcp"),
    stateless_http=True,
)


@mcp.tool()
def health() -> dict[str, Any]:
    """Return basic service status and configured upstream endpoints."""
    return {
        "status": "ok",
        "service": "codeguardian-mcp",
        "trigger_phrases": {
            "code review": "code_review",
            "code improvement": "code_improvement",
            "code improvements": "code_improvements",
            "apply code review selection": "apply_code_review_changes",
            "apply code improvements selection": "apply_code_improvement_changes",
        },
        "bitbucket_url": os.getenv("BITBUCKET_URL", "https://api.bitbucket.org/2.0"),
        "sonarqube_url": os.getenv("SONARQUBE_URL", "http://sonarqube-server:9000"),
        "prometheus_url": os.getenv("PROMETHEUS_URL", "http://prometheus:9090"),
        "jenkins_url": os.getenv("JENKINS_URL", "http://jenkins-blueocean:8080"),
    }


@mcp.tool()
def get_sonarqube_findings(
    project_key: str,
    severities: str = "",
    resolved: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """List SonarQube findings for a project key, optionally filtered by severities."""
    return mcp_tools.list_sonarqube_findings(project_key, severities, resolved, limit)


@mcp.tool()
async def get_pr_review_status(pr_id: int, workspace: str = "", repo_slug: str = "") -> dict[str, Any]:
    """Return Bitbucket PR metadata plus CodeGuardian inline comment counts and bodies."""
    return await mcp_tools.get_pr_review_status_async(workspace, repo_slug, pr_id)


@mcp.tool()
async def list_codeguardian_comments(pr_id: int, workspace: str = "", repo_slug: str = "", limit: int = 100) -> str:
    """Use this when the user asks to list comments. Returns English markdown for every CodeGuardian comment."""
    result = await mcp_tools.list_bitbucket_codeguardian_comments_async(workspace, repo_slug, pr_id, limit)
    return result["markdown"]


@mcp.tool()
async def list_open_pull_requests(workspace: str = "", repo_slug: str = "") -> dict[str, Any]:
    """List open pull requests for a Bitbucket repository."""
    return await mcp_tools.list_open_pull_requests_async(workspace, repo_slug)


@mcp.tool()
async def list_comments_for_open_pr(workspace: str = "", repo_slug: str = "") -> str:
    """Use this for simple requests like 'review the comments of the open PR' or 'tell me the comments of the open PR'. It detects the repo from local Git remotes when repo_slug is omitted and returns the numbered review flow that asks which suggestions to apply."""
    return await mcp_tools.list_comments_for_open_pr_async(workspace, repo_slug)


@mcp.tool()
async def review_codeguardian_suggestions(
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int = 0,
    limit: int = 100,
    start: int = 1,
    count: int = 1,
) -> str:
    """Use this when the user wants to review comments and decide changes to apply, including simple phrases about comments from the open PR. Return a small page of suggestions verbatim, defaulting to one complete suggestion, with File, Line, Original code, Explanation and Proposed code. Then ask whether to accept or skip."""
    return await mcp_tools.review_codeguardian_suggestions_async(workspace, repo_slug, pr_id, limit, start, count)


@mcp.tool()
async def code_review(
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int = 0,
    count: int = 3,
) -> str:
    """MANDATORY for trigger phrase 'code review'. Do not manually inspect files. Shows the first pending SonarQube problem suggestions, up to 3, with File, Line, Original code, Explanation and Proposed code."""
    return await mcp_tools.codeguardian_batch_async(
        "review",
        workspace=workspace,
        repo_slug=repo_slug,
        pr_id=pr_id,
        count=count,
    )


@mcp.tool()
async def code_improvements(
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int = 0,
    count: int = 3,
) -> str:
    """MANDATORY for trigger phrase 'code improvements'. Do not manually inspect files. Shows the first pending optimization suggestions, up to 3, with File, Line, Original code, Explanation and Proposed code."""
    return await mcp_tools.codeguardian_batch_async(
        "improvements",
        workspace=workspace,
        repo_slug=repo_slug,
        pr_id=pr_id,
        count=count,
    )


@mcp.tool()
async def code_improvement(
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int = 0,
    count: int = 3,
) -> str:
    """MANDATORY for trigger phrase 'code improvement'. Alias for code_improvements. Do not manually inspect files."""
    return await mcp_tools.codeguardian_batch_async(
        "improvements",
        workspace=workspace,
        repo_slug=repo_slug,
        pr_id=pr_id,
        count=count,
    )


@mcp.tool()
async def apply_codeguardian_comment_replacement(
    pr_id: int,
    comment_id: int,
    workspace: str = "",
    repo_slug: str = "",
    local_repo_path: str = "",
) -> dict[str, Any]:
    """Apply one CodeGuardian comment replacement directly to the mounted local repository."""
    return await mcp_tools.apply_codeguardian_comment_replacement_async(
        workspace,
        repo_slug,
        pr_id,
        comment_id,
        local_repo_path,
    )


@mcp.tool()
async def apply_approved_codeguardian_suggestions(
    selection: str,
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int = 0,
    local_repo_path: str = "",
) -> dict[str, Any]:
    """Write tool. Apply developer-approved CodeGuardian suggestion numbers from the review output directly to the mounted local workspace after the developer confirms."""
    return await mcp_tools.apply_approved_codeguardian_suggestions_async(
        selection,
        workspace,
        repo_slug,
        pr_id,
        local_repo_path,
    )


@mcp.tool()
async def apply_code_review_changes(
    selection: str,
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int = 0,
) -> dict[str, Any]:
    """MANDATORY after the developer selects visible code_review items. Apply directly to the local workspace. Selection can be 1, 2, 3, combinations like '1 and 3', or 'all'."""
    return await mcp_tools.apply_codeguardian_batch_selection_async(
        "review",
        selection,
        workspace=workspace,
        repo_slug=repo_slug,
        pr_id=pr_id,
    )


@mcp.tool()
async def apply_code_improvement_changes(
    selection: str,
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int = 0,
) -> dict[str, Any]:
    """MANDATORY after the developer selects visible code_improvement/code_improvements items. Apply directly to the local workspace. Selection can be 1, 2, 3, combinations like '1 and 3', or 'all'."""
    return await mcp_tools.apply_codeguardian_batch_selection_async(
        "improvements",
        selection,
        workspace=workspace,
        repo_slug=repo_slug,
        pr_id=pr_id,
    )


@mcp.tool()
def get_codeguardian_metrics(repository: str = "") -> dict[str, Any]:
    """Read CodeGuardian metrics from Prometheus for one repository or all repositories."""
    return mcp_tools.get_codeguardian_metrics(repository)


@mcp.tool()
def query_prometheus(query: str) -> dict[str, Any]:
    """Run a read-only Prometheus instant query."""
    return mcp_tools.query_prometheus(query)


@mcp.tool()
def get_jenkins_build_summary(job_name: str, build_number: str = "lastBuild") -> dict[str, Any]:
    """Return a Jenkins build summary for a job name and build number."""
    return mcp_tools.get_jenkins_build_summary(job_name, build_number)


def main() -> None:
    transport = cast(McpTransport, os.getenv("CODEGUARDIAN_MCP_TRANSPORT", "streamable-http"))
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
