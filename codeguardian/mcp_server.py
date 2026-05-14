import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from codeguardian import mcp_tools


def _server_port() -> int:
    try:
        return int(os.getenv("CODEGUARDIAN_MCP_PORT", "8010"))
    except ValueError:
        return 8010


mcp = FastMCP(
    "CodeGuardian",
    instructions=(
        "Tools for consulting CodeGuardian PR reviews, SonarQube findings, Bitbucket inline "
        "comments, Prometheus metrics and Jenkins build summaries. Replacement application "
        "is local-workspace only and should be run with dry_run first."
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
    """Use this when the user asks for comments from the open PR of this repository. Detects the repo from local Git remotes when repo_slug is omitted."""
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
    """Use this for editor-only review. Return a small page of suggestions verbatim, defaulting to one complete suggestion, with File, Line, Original code, Explanation and Proposed code. Then ask whether to accept or skip."""
    return await mcp_tools.review_codeguardian_suggestions_async(workspace, repo_slug, pr_id, limit, start, count)


@mcp.tool()
async def apply_codeguardian_comment_replacement(
    pr_id: int,
    comment_id: int,
    workspace: str = "",
    repo_slug: str = "",
    local_repo_path: str = "",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Apply one CodeGuardian comment replacement to the mounted local repository."""
    return await mcp_tools.apply_codeguardian_comment_replacement_async(
        workspace,
        repo_slug,
        pr_id,
        comment_id,
        local_repo_path,
        dry_run,
    )


@mcp.tool()
async def apply_approved_codeguardian_suggestions(
    selection: str,
    workspace: str = "",
    repo_slug: str = "",
    pr_id: int = 0,
    local_repo_path: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write tool. Apply developer-approved CodeGuardian suggestion numbers from the review output to the mounted local workspace. Copilot should request user approval before calling."""
    return await mcp_tools.apply_approved_codeguardian_suggestions_async(
        selection,
        workspace,
        repo_slug,
        pr_id,
        local_repo_path,
        dry_run,
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
    mcp.run(transport=os.getenv("CODEGUARDIAN_MCP_TRANSPORT", "streamable-http"))


if __name__ == "__main__":
    main()
