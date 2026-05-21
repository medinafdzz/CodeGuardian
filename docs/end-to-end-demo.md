# End-to-End Demo

## Objective

This document describes the complete demo flow for CodeGuardian. The objective is to have a repeatable execution that can be used for the final degree project presentation and for taking screenshots for the report.

The demo shows the complete path:

```text
Bitbucket pull request
  -> Jenkins pipeline
  -> SonarQube analysis
  -> CodeGuardian agent
  -> Bitbucket inline comments
  -> Pushgateway metrics
  -> Prometheus
  -> Grafana dashboard
```

The demo target repository is `codeguardian-sample-mixed`, but the system is not limited to that repository. `codeguardian-sample-mixed` is only the controlled project used to demonstrate the workflow with Java and Python code.

## Repositories Used

| Repository | Role in the demo |
| --- | --- |
| `codeguardian-infra` | Starts Jenkins, SonarQube, Prometheus, Pushgateway and Grafana |
| `codeguardian-core` | Contains the Python agent executed by Jenkins |
| `codeguardian-sample-mixed` | Demo target repository analysed by the pipeline |

## Previous Requirements

Before starting the demo, check that:

- Docker is running.
- The infrastructure volumes exist.
- Jenkins credentials are configured.
- SonarQube has a valid token.
- Bitbucket credentials are valid.
- The Atlassian MCP authentication header is configured.
- The branch used by the Jenkinsfile points to `codeguardian-core` `main`.

The most important Jenkins credentials are:

| Credential ID | Purpose |
| --- | --- |
| `sonarqube-token` | Allows Jenkins and the agent to access SonarQube |
| `LLM-token` | Allows the agent to call the AI model |
| `bitbucket_email` | Bitbucket account email |
| `bitbucket-token` | Bitbucket API token |
| `atlassian-mcp-auth-header` | Authentication header for Atlassian MCP |

## 1. Start The Infrastructure

From `codeguardian-infra`, start the environment:

```bash
docker compose up -d --build
```

Check the containers:

```bash
docker compose ps
```

Expected running services:

- `jenkins-blueocean`
- `sonarqube-server`
- `prometheus`
- `pushgateway`
- `grafana`

## 2. Check Service URLs

Open the local services:

| Service | URL |
| --- | --- |
| Jenkins | <http://localhost:8080> |
| SonarQube | <http://localhost:9000> |
| Prometheus | <http://localhost:9090> |
| Pushgateway | <http://localhost:9091> |
| Grafana | <http://localhost:3000> |

Grafana credentials:

```text
user: admin
password: admin
```

## 3. Check Jenkins Job

In Jenkins, use the `CodeGuardian` job. This job should discover the pull requests from the Bitbucket repository.

For the demo repository, the expected branch flow is usually:

```text
main <- demo/mixed-review
```

The pull request must contain changes that can generate SonarQube findings. If the repository has no relevant findings, the agent will report a clean analysis state.

## 4. Run The Pipeline

Run the pull request job from Jenkins.

The expected pipeline stages are:

```text
Analyze PR
Run AI agent
Declarative: Post Actions
```

During `Analyze PR`, Jenkins should:

- checkout the pull request,
- detect the project type,
- compile the project when the project type supports it,
- run SonarQube analysis.

During `Run AI agent`, Jenkins should:

- clone `codeguardian-core`,
- create `data.json`,
- execute `python3 -u AIagent/agent.py --file data.json`,
- send the final comments to Bitbucket,
- push metrics to Pushgateway.

## 5. Expected Jenkins Result

The expected final result is:

```text
Finished: SUCCESS
```

Useful log lines to check:

```text
Analysis completed for <repository> (<project_type>)
Relevant issues found by SonarQube
Gemini produced <n> issues
Dropped <n> issues after patch validation
Execution summary: sonar_findings=...
Inline synchronization summary: desired=... created=... reused=... deleted=...
Execution metrics pushed to Prometheus Pushgateway
Comments synchronized
```

If the job fails, the Jenkins log should be used as the main debugging source.

## 6. Check SonarQube

Open SonarQube:

```text
http://localhost:9000
```

Check that the project appears with the expected project key. In the demo, this is normally the repository name.

The important part for the demo is to show that:

- the repository was analysed,
- issues were detected,
- Jenkins used those issues as input for the agent.

## 7. Check Bitbucket Pull Request

Open the pull request in Bitbucket.

Expected result:

- CodeGuardian inline comments appear in the changed files.
- Old obsolete CodeGuardian comments are removed.
- Existing matching comments are reused.
- The comments contain the problem, the proposed solution and the suggested code replacement.

This proves that the agent does not only write logs, but also integrates with the pull request review flow.

## 8. Check Pushgateway And Prometheus

Open Pushgateway:

```text
http://localhost:9091
```

Search for metrics with names starting with:

```text
codeguardian_
```

Open Prometheus:

```text
http://localhost:9090
```

Example queries:

```promql
codeguardian_analysis_latency_seconds
codeguardian_analysis_total_tokens
codeguardian_sonar_findings_total
codeguardian_final_issues_total
codeguardian_comments_created_total
```

If these metrics appear, the observability flow is working.

## 9. Check Grafana Dashboard

Open Grafana:

```text
http://localhost:3000
```

The dashboard is provisioned automatically:

```text
CodeGuardian / CodeGuardian Overview
```

Expected panels:

- analysis latency,
- token usage,
- issue flow,
- batch cache,
- Bitbucket comment synchronization,
- blocking finding indicator.

If the dashboard appears but it is empty, run a Jenkins build first. Grafana can only show data after the agent has pushed metrics.

## 10. Screenshot Checklist

For the final report, the recommended screenshots are:

| Screenshot | What it proves |
| --- | --- |
| Docker containers running | The infrastructure is active |
| Jenkins job configuration or job list | The CI/CD entry point exists |
| Jenkins successful build | The complete pipeline works |
| Jenkins log with execution summary | The agent processed findings and validation |
| SonarQube project page | Static analysis was executed |
| Bitbucket pull request comments | The agent published review feedback |
| Pushgateway metrics page | The agent exported metrics |
| Prometheus query result | Metrics are collected |
| Grafana `CodeGuardian Overview` dashboard | Metrics are visualized |

These screenshots should be taken after a successful Jenkins execution.

## 11. Demo Success Criteria

The demo can be considered valid when:

- all infrastructure containers are running,
- Jenkins finishes the pull request job with success,
- SonarQube contains the analysed project,
- Bitbucket shows CodeGuardian inline comments,
- Pushgateway contains `codeguardian_` metrics,
- Prometheus can query those metrics,
- Grafana shows the `CodeGuardian Overview` dashboard with data.

## 12. Common Problems

### Jenkins cannot reach SonarQube

From Jenkins, the correct URL is:

```text
http://sonarqube-server:9000
```

Do not use `localhost:9000` inside Jenkins.

### Grafana dashboard is empty

Run a Jenkins build first. The dashboard depends on metrics pushed by the agent.

### Bitbucket comments are not created

Check:

- `BITBUCKET_EMAIL`,
- `BITBUCKET_API_TOKEN`,
- `ATLASSIAN_MCP_AUTH_HEADER`,
- repository slug,
- workspace,
- pull request ID.

### Agent branch is not updated

Check the `AGENT_REPO_REF` variable in the demo `Jenkinsfile`. Jenkins clones that branch from `codeguardian-core`.

## Conclusion

This demo closes the practical execution path of CodeGuardian. It shows the integration between CI/CD, static analysis, AI-assisted review, validation, pull request comments and observability.

For the final degree project, this is the main evidence that the system is not only a local script, but a complete automated review workflow.
