# CodeGuardian Infra

This repository contains the infrastructure of CodeGuardian. It provides the Docker environment required to execute the end-to-end demo of the final degree project: Jenkins runs the pipeline, SonarQube detects quality problems, the CodeGuardian agent generates AI suggestions, and Prometheus/Grafana are used to observe the execution.

The objective of this repository is not to contain the agent logic, but to provide the execution environment where the complete system can be tested and demonstrated.

## Components

| Component | Container | Port | Function |
| --- | --- | --- | --- |
| Jenkins + Blue Ocean | `jenkins-blueocean` | `8080`, `50000` | Executes pull request pipelines and starts the agent |
| SonarQube Community | `sonarqube-server` | `9000` | Analyses the code and exposes the issues used by the agent |
| Prometheus | `prometheus` | `9090` | Collects metrics from Pushgateway |
| Pushgateway | `pushgateway` | `9091` | Receives metrics from short agent executions |
| Grafana | `grafana` | `3000` | Shows execution metrics in dashboards |

All services are connected to the Docker network `services-net`.

## Relation With The Other Repositories

CodeGuardian is divided into three repositories:

- `codeguardian-infra`: this repository. It defines Jenkins, SonarQube, Prometheus, Pushgateway and Grafana.
- `codeguardian-core`: contains the Python agent that reads SonarQube issues, calls the AI model, validates suggestions and publishes comments in Bitbucket.
- `demo-java` / `app-demo`: Java repository used as the target project for the demonstration.

Expected execution flow:

```text
Pull Request in demo-java
  -> Jenkins executes the Jenkinsfile
  -> SonarQube analyses the code
  -> Jenkins clones codeguardian-core
  -> agent.py reads issues, generates suggestions and validates results
  -> Bitbucket receives inline comments
  -> Pushgateway receives metrics
  -> Prometheus/Grafana show observability data
```

## Requirements

Before starting the environment, the following elements are needed:

- Docker installed.
- Docker Compose available.
- Access to Bitbucket.
- SonarQube token.
- Token or credentials for the LLM provider used by the agent.
- Bitbucket API token.
- Authentication header for Atlassian MCP.

In Linux, Jenkins uses the Docker socket from the host:

```yaml
/var/run/docker.sock:/var/run/docker.sock
```

For this reason, the Jenkins container is added to groups `999` and `0` in `compose.yaml`. If the Docker group in the host has another identifier, it can be necessary to modify `group_add`.

## Initial Volume Preparation

The `compose.yaml` file declares some volumes as external:

```yaml
jenkins-data
sonarqube_logs
sonarqube_data
sonarqube_temp
sonarqube_extensions
```

If these volumes do not exist, create them before starting the environment:

```bash
docker volume create infraestructura_jenkins-data
docker volume create infraestructura_sonarqube_logs
docker volume create infraestructura_sonarqube_data
docker volume create infraestructura_sonarqube_temp
docker volume create infraestructura_sonarqube_extensions
```

Prometheus, Pushgateway and Grafana data are stored in local folders:

```text
prometheus_data/
pushgateway_data/
grafana_data/
```

## Start The Environment

Build and start all services:

```bash
docker compose up -d --build
```

Check the status:

```bash
docker compose ps
```

Read logs:

```bash
docker compose logs -f jenkins-blueocean
docker compose logs -f sonarqube-server
```

Stop the environment:

```bash
docker compose down
```

Stop containers and remove orphan containers without deleting volumes:

```bash
docker compose down --remove-orphans
```

## Local URLs

| Service | URL |
| --- | --- |
| Jenkins | <http://localhost:8080> |
| SonarQube | <http://localhost:9000> |
| Prometheus | <http://localhost:9090> |
| Pushgateway | <http://localhost:9091> |
| Grafana | <http://localhost:3000> |

Grafana uses the administrator password configured in `compose.yaml`:

```text
user: admin
password: admin
```

For a real environment, `GF_SECURITY_ADMIN_PASSWORD` should be changed.

## Jenkins Image

The `Dockerfile` builds an image based on:

```dockerfile
jenkins/jenkins:2.541.3-jdk21
```

The image includes the tools needed to analyse different types of projects:

- Docker CLI.
- Python 3, pip and venv.
- Maven and Gradle.
- Node.js and npm.
- GCC, G++, Make, CMake and Ninja.
- Clang, clang-tidy, cppcheck, gdb and valgrind.
- ShellCheck.
- `@sonar/scan`.
- `mcp-sonarqube`.
- `bitbucket-mcp`.
- Python packages used by the agent: `google-genai`, `mcp`, `prometheus-client`, `pydantic`.
- Jenkins plugins: Blue Ocean, Docker Workflow and JSON Path API.

With this image, Jenkins can execute analysis pipelines for Java, Gradle, Node, Python and C/C++ projects, depending on the pipeline configuration.

## Jenkins Configuration

Jenkins must contain the credentials used by the `Jenkinsfile` of the demo repository.

Expected credentials:

| Jenkins ID | Recommended type | Use |
| --- | --- | --- |
| `sonarqube-token` | Secret text | Authentication against SonarQube |
| `LLM-token` | Secret text | Authentication against the AI provider |
| `bitbucket_email` | Secret text | Email of the Bitbucket account |
| `bitbucket-token` | Secret text | Bitbucket API token |
| `atlassian-mcp-auth-header` | Secret text | Authentication header for Atlassian MCP |

There must also be a SonarQube configuration in Jenkins with this name:

```text
SonarQube-Server
```

The demo `Jenkinsfile` uses this configuration with:

```groovy
withSonarQubeEnv('SonarQube-Server')
```

## SonarQube Configuration

Minimum steps:

1. Access <http://localhost:9000>.
2. Create or recover a user token.
3. Register that token in Jenkins as `sonarqube-token`.
4. Check that Jenkins can reach SonarQube using the internal URL:

```text
http://sonarqube-server:9000
```

Inside Docker, Jenkins must not use `localhost:9000` to communicate with SonarQube, because `localhost` would point to the Jenkins container itself.

## Observability

The agent from `codeguardian-core` sends metrics to Pushgateway. Prometheus collects them using the configuration from `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'pushgateway'
    honor_labels: true
    static_configs:
      - targets: ['pushgateway:9091']
```

Metrics flow:

```text
agent.py -> Pushgateway -> Prometheus -> Grafana
```

Grafana is provisioned automatically from the files under:

```text
grafana/provisioning/
grafana/dashboards/
```

This means the Prometheus data source and the `CodeGuardian Overview` dashboard are created when the Grafana container starts. They do not need to be created manually from the web interface.

Current expected metrics:

- analysis latency,
- last execution timestamp,
- prompt tokens,
- response tokens,
- total tokens,
- cached tokens,
- batch cache hits and misses,
- SonarQube findings,
- generated issues,
- invalid generated issues,
- patch validation drops,
- final issues,
- blocking findings,
- desired comments,
- created comments,
- reused comments,
- deleted comments.

The provisioned dashboard includes panels for:

- analysis latency,
- token usage,
- issue flow,
- batch cache behaviour,
- Bitbucket comment synchronization,
- blocking finding indicator.

The dashboard uses approximate colour thresholds for the demo:

| Panel | Green | Yellow | Red |
| --- | --- | --- | --- |
| Analysis latency | `< 120 s` | `120-300 s` | `> 300 s` |
| Token usage | `< 50k` | `50k-150k` | `> 150k` |
| Final issues | `0-5` | `5-15` | `> 15` |
| Blocking findings | `0` | - | `>= 1` |
| Comment synchronization | `< 10` | `10-30` | `> 30` |
| Batch cache activity | `< 10` | `10-30` | `> 30` |

These thresholds are not strict production limits. They are practical limits for the TFG demo, used to identify executions that are normal, heavy or potentially problematic.

Recommended metrics for future iterations:

- errors by external system,
- per-batch latency,
- per-batch token usage,
- repository-level historical aggregates.

## End-To-End Execution

Recommended sequence for the demo:

1. Start this infrastructure.
2. Configure Jenkins and SonarQube.
3. Create a multibranch job or pipeline for `demo-java`.
4. Open a pull request in Bitbucket over `demo-java`.
5. Execute the pipeline.
6. Check that SonarQube analyses the project.
7. Check that Jenkins clones `codeguardian-core`.
8. Check that the agent publishes inline comments in Bitbucket.
9. Review metrics in Prometheus or Grafana.

The complete repeatable demo guide is available in:

- [docs/end-to-end-demo.md](docs/end-to-end-demo.md)

## Relevant Pipeline Variables

The `Jenkinsfile` of the demo repository defines variables like:

| Variable | Usual value | Description |
| --- | --- | --- |
| `SONARQUBE_HOST_URL` | `http://sonarqube-server:9000` | Internal SonarQube URL |
| `AGENT_REPO_URL` | URL of `codeguardian-core` | Agent repository |
| `AGENT_REPO_REF` | Agent branch | Branch cloned by Jenkins |
| `BITBUCKET_WORKSPACE` | Bitbucket workspace | Workspace where the PR exists |
| `CACHE_MODE` | `explicit` | Agent cache mode |
| `CACHE_TTL` | `3600s` | Cache time to live |

## Troubleshooting

### Jenkins Cannot Use Docker

Check that the socket is mounted:

```bash
docker compose exec jenkins-blueocean ls -l /var/run/docker.sock
```

If there are permission errors, review `group_add` in `compose.yaml` and the real GID of the Docker group in the host.

### Jenkins Cannot Reach SonarQube

From the Jenkins container:

```bash
docker compose exec jenkins-blueocean curl -I http://sonarqube-server:9000
```

Do not use `localhost:9000` from Jenkins.

### Prometheus Does Not Show Metrics

Check Pushgateway:

```bash
curl http://localhost:9091/metrics
```

Check the target in Prometheus:

```text
http://localhost:9090/targets
```

### Grafana Does Not Show Data

Prometheus is provisioned automatically as the default data source:

```text
URL: http://prometheus:9090
```

If the dashboard does not appear, restart Grafana:

```bash
docker compose restart grafana
```

If the dashboard appears but panels are empty, execute a Jenkins build first. The agent must push metrics to Pushgateway before Grafana can show data.

If a data source is configured manually, do not use `localhost:9090`. Grafana runs inside a container, so it must use the internal service name `http://prometheus:9090`.

If the dashboard shows too many duplicated series, clear old Pushgateway groups and execute the Jenkins build again:

```bash
curl -X DELETE http://localhost:9091/metrics/job/codeguardian_agent
```

This can happen after changing the metric labels, because Pushgateway keeps old pushed series until they are deleted.

### SonarQube Takes Time To Start

SonarQube can need some minutes to be available. Check:

```bash
docker compose logs -f sonarqube-server
```

### The Agent Does Not Publish Comments

Review:

- the pipeline is executed on a pull request,
- there are issues in SonarQube,
- `BITBUCKET_EMAIL` and `BITBUCKET_API_TOKEN` are valid,
- `ATLASSIAN_MCP_AUTH_HEADER` is configured,
- the repository and workspace sent in `data.json` match Bitbucket.

## Repository Status

This repository provides the base infrastructure to execute CodeGuardian in a local or laboratory environment. Its main objective inside the final degree project is to make the solution reproducible, observable and demonstrable from end to end.
