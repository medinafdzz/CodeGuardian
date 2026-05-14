# CodeGuardian MCP for Copilot

CodeGuardian includes an MCP server intended for GitHub Copilot Business or Enterprise. It exposes review context from the TFG infrastructure and can optionally apply a single CodeGuardian replacement to a mounted local workspace.

## What it connects to

The MCP server runs as `codeguardian-mcp` in `codeguardian-infra` and connects to:

- Atlassian Rovo MCP, to read Bitbucket PR metadata and CodeGuardian inline comments.
- SonarQube, to read current findings for a project key.
- Prometheus, to read CodeGuardian execution metrics.
- Jenkins, to read build summaries.

The server uses the infrastructure network names:

- `http://sonarqube-server:9000`
- `http://prometheus:9090`
- `http://jenkins-blueocean:8080`

From the host, Copilot connects to:

```text
http://localhost:8010/mcp
```

## Required environment variables

Set these before running `docker compose up` from `codeguardian-infra`:

```bash
BITBUCKET_EMAIL=your.email@company.com
BITBUCKET_API_TOKEN=your_bitbucket_token
ATLASSIAN_MCP_AUTH_HEADER="Basic your_base64_atlassian_credentials"
BITBUCKET_WORKSPACE=your_workspace
SONARQUBE_AUTH_TOKEN=your_sonarqube_token
JENKINS_USER=your_jenkins_user
JENKINS_API_TOKEN=your_jenkins_api_token
```

`ATLASSIAN_MCP_AUTH_HEADER` is required for Bitbucket PR and comment reads through Atlassian Rovo MCP. `BITBUCKET_EMAIL` and `BITBUCKET_API_TOKEN` are still used by the agent Bitbucket REST write path. `JENKINS_USER` and `JENKINS_API_TOKEN` are optional for public/readable Jenkins endpoints, but they are recommended.

`BITBUCKET_WORKSPACE` is optional. When `repo_slug` is omitted, CodeGuardian detects Bitbucket repositories from the Git origin remotes mounted under `CODEGUARDIAN_WORKSPACE_ROOT` and searches their open PRs. It does not use a fixed repository default.

## Available tools

- `health`: shows the configured upstream endpoints.
- `get_sonarqube_findings`: lists SonarQube issues for a project key.
- `get_pr_review_status`: reads Bitbucket PR metadata and CodeGuardian comment counts through Atlassian MCP.
- `list_codeguardian_comments`: lists every CodeGuardian inline comment in a PR with file, line, problem, original code, proposal and proposed code.
- `list_open_pull_requests`: lists open Bitbucket pull requests in a repository through Atlassian MCP.
- `list_comments_for_open_pr`: lists every CodeGuardian comment from the only open PR, or asks the user to choose when more than one PR is open.
- `code_review`: use when the developer writes `code review`. It shows up to 3 pending SonarQube problem fixes, skipping items already applied locally.
- `code_improvement` / `code_improvements`: use when the developer writes `code improvement` or `code improvements`. It shows up to 3 pending optimization suggestions, skipping items already applied locally.
- `apply_code_review_changes`: applies the selected visible `code review` items directly to the mounted local repository.
- `apply_code_improvement_changes`: applies the selected visible `code improvements` items directly to the mounted local repository.
- `review_codeguardian_suggestions`: shows replaceable suggestions numbered for editor approval. It defaults to one complete suggestion at a time so Copilot does not summarize away the original code.
- `apply_approved_codeguardian_suggestions`: applies the suggestion numbers approved by the developer to the mounted local repository.
- `apply_codeguardian_comment_replacement`: low-level tool that applies one comment replacement by Bitbucket comment ID.
- `get_codeguardian_metrics`: reads CodeGuardian metrics from Prometheus.
- `query_prometheus`: runs a read-only Prometheus instant query.
- `get_jenkins_build_summary`: reads a Jenkins build summary.

## Copilot configuration

In VS Code, configure an MCP server that points to the streamable HTTP endpoint:

```json
{
  "servers": {
    "codeguardian": {
      "type": "http",
      "url": "http://localhost:8010/mcp"
    }
  }
}
```

Depending on your VS Code/Copilot version, this can be added through the MCP server UI or a workspace/user MCP configuration file.

## Example prompts

```text
Use the CodeGuardian MCP server. How many SonarQube issues are open for sample-mixed?
```

```text
Use CodeGuardian to summarize PR 4 in workspace medinafdzz repository sample-mixed.
```

```text
List the CodeGuardian optimization comments in PR 4 of sample-mixed.
```

```text
Use CodeGuardian. List all comments for PR 1 of sample-mixed in workspace medinafdzz.
```

```text
Use CodeGuardian. List the comments from the open PR of sample-mixed in workspace medinafdzz.
```

```text
Use CodeGuardian. code review for PR 1 of sample-mixed in workspace medinafdzz.
```

```text
Use CodeGuardian. code improvements for PR 1 of sample-mixed in workspace medinafdzz.
```

```text
Apply 1 and 3.
```

```text
Compare the latest CodeGuardian metrics for sample-java and sample-python.
```

```text
Show the latest Jenkins build result for the sample-mixed organization folder job.
```

## Replacement safety model

Most tools are read-only. `apply_code_review_changes`, `apply_code_improvement_changes`, `apply_approved_codeguardian_suggestions` and `apply_codeguardian_comment_replacement` are the only tools that can write files, and they only write to the mounted local workspace configured by `CODEGUARDIAN_WORKSPACE_ROOT`.

The editor approval flow is:

- For SonarQube problems, the developer writes `code review` and Copilot calls `code_review`.
- For optimization suggestions, the developer writes `code improvement` or `code improvements` and Copilot calls `code_improvements`.
- CodeGuardian returns the first 3 pending items with file, line, original code, explanation and proposed code.
- Items already applied locally are skipped because their original code block no longer exists in the workspace.
- The developer chooses `1`, `2`, `3`, combinations like `1 and 3`, or `all`.
- Copilot calls `apply_code_review_changes` or `apply_code_improvement_changes` with the accepted selection.
- VS Code/Copilot asks for tool execution approval before files are modified.

The replacement tools:

- reads one existing CodeGuardian Bitbucket comment,
- extracts `Block to substitute` and `Proposed Code`,
- resolves the target file inside the mounted local repository,
- requires the current code block to appear exactly once,
- does not commit, push, approve PRs, create Bitbucket comments or trigger Jenkins jobs.
