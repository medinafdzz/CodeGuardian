# Pipeline Workflow

## Introduction

The Jenkins pipeline is the execution backbone of CodeGuardian. Its role is to take a pull request event, prepare the repository under review, run static analysis with SonarQube, and then execute the CodeGuardian agent with the minimum context required to publish review comments back into Bitbucket.

The pipeline is intentionally simple in structure. Instead of trying to solve every task inside Jenkins itself, it focuses on orchestration: checkout, project detection, SonarQube execution, agent launch, and workspace cleanup. This keeps the CI/CD flow easier to understand and easier to maintain.

---

## Pipeline Purpose

The purpose of the pipeline is to automate the review flow for pull requests. In practical terms, it does four main things:

1. checks out the pull request code,
2. detects the repository type,
3. runs the appropriate SonarQube analysis path,
4. launches the CodeGuardian agent with the pull request metadata.

This makes Jenkins the coordination layer between Bitbucket, SonarQube and the Python agent.

---

## Global Configuration

The pipeline starts with a global configuration block that defines the execution environment and the main behaviour rules.

### Agent

The pipeline runs with:

- `agent any`

This means Jenkins can execute the job on any available agent that satisfies the required tooling.

### Environment variables

Several environment variables are defined at pipeline level:

- `SONARQUBE_HOST_URL`
- `AGENT_REPO_URL`
- `AGENT_REPO_REF`
- `BITBUCKET_WORKSPACE`

These values provide the base configuration for the SonarQube server, the external repository where the agent is stored, the branch of that repository to clone, and the Bitbucket workspace used later by the agent.

### Options

The pipeline also sets a few useful execution options:

- `skipDefaultCheckout()`
- `disableConcurrentBuilds()`
- `buildDiscarder(logRotator(numToKeepStr: '20'))`

These options are important for reliability and maintenance:

- the default checkout is skipped because checkout is controlled explicitly,
- concurrent builds are disabled to avoid collisions or duplicated review actions,
- and old builds are discarded automatically to limit Jenkins storage growth.

---

## Stage Structure

The pipeline is divided into two main stages:

1. **Analyze PR**
2. **Run AI agent**

Both stages are guarded with `changeRequest()`, which means they are only executed for pull request builds.

This is an important design decision. The pipeline is not intended to behave like a generic push pipeline. Its purpose is specifically tied to pull request review.

---

## Stage 1: Analyze PR

## Purpose

The first stage is responsible for preparing the repository under review and launching SonarQube analysis with a configuration adapted to the detected project type.

## Checkout

The stage begins with:

- `checkout scm`

This checks out the pull request source in the workspace so the rest of the stage works against the repository currently being reviewed.

## Repository metadata

Inside the script block, the pipeline computes two basic values:

- `REPO_NAME`
- `SONARQUBE_PROJECT_KEY`

`REPO_NAME` is extracted from the Git remote URL, and `SONARQUBE_PROJECT_KEY` is derived from it. This gives the pipeline a stable way to identify the repository both for SonarQube analysis and for the agent execution.

## Exclusion patterns

The stage defines three groups of exclusions:

- `commonExclusions`
- `pythonExclusions`
- `cfamilyExclusions`

These exclusions prevent SonarQube from scanning generated folders, build outputs, dependency directories or local virtual environments that would only add noise to the analysis.

## Project type detection

One of the most important pieces of logic in this stage is project type detection.

The pipeline checks for the presence of common build or package files and maps the repository into one of these categories:

- `maven`
- `gradle`
- `node`
- `python`
- `cfamily_compile_db`
- `cfamily`
- `generic`

The detection is based on simple repository signals such as:

- `pom.xml`
- `build.gradle`, `build.gradle.kts`, `gradlew`
- `package.json`
- `pyproject.toml`, `requirements.txt`, `setup.py`
- `compile_commands.json`
- `CMakeLists.txt`
- `Makefile`

This is a practical approach because it keeps the pipeline generic and avoids hardcoding behaviour per repository.

## Quiet command execution

The helper closure `runQuiet` is used to execute build and scanner commands while redirecting output to log files. If the command fails, Jenkins prints the last 200 lines of the corresponding log.

This is a good compromise between:
- not flooding the Jenkins console with full command output,
- and still preserving enough information for debugging when something goes wrong.

## SonarQube execution

Inside `withSonarQubeEnv('SonarQube-Server')`, the pipeline defines a helper closure named `runScanner` that launches `sonar-scanner` with a shared base configuration and some additional arguments depending on the project type.

The common scanner configuration includes:

- project key
- project name
- SonarQube URL
- SCM exclusions enabled
- no quality gate wait
- warning log level

The project-specific behaviour is then selected with a `switch` statement.

### Maven

For Maven projects, the pipeline runs:

- `mvn -B -q -ntp -DskipTests clean compile`

and then launches SonarQube with:
- `src/main/java` as source path,
- `target/classes` as Java binaries path,
- and the common exclusion set.

### Gradle

For Gradle projects, the pipeline checks whether `gradlew` exists.

- If it exists, it uses the wrapper.
- Otherwise, it falls back to the system `gradle` command.

The build target is:
- `classes -x test`

and SonarQube is configured with:
- `src/main/java` as source path,
- `build/classes` as binaries path,
- and the common exclusion set.

### Node

For Node projects, the pipeline does not perform an explicit build here. It directly runs SonarQube over the repository using the common exclusions.

### Python

For Python projects, the pipeline also runs SonarQube directly over the repository, but with Python-specific exclusions to avoid scanning virtual environments and cache directories.

### C/C++ with compile database

If `compile_commands.json` is available, the pipeline uses it through:

- `sonar.cfamily.compile-commands=compile_commands.json`

This is the preferred path for C and C++ projects because it gives SonarQube the compilation metadata required for a more accurate analysis.

### C/C++ without compile database

If the project looks like a C or C++ repository but `compile_commands.json` is missing, the pipeline stops with an explicit error.

This is a sensible design choice. Instead of pretending to analyse the project with incomplete information, the pipeline fails early and makes the missing requirement clear.

### Generic repositories

For any repository that does not match the known project types, the pipeline falls back to a generic SonarQube scan over the repository using the common exclusions.

## Stage output

At the end of the stage, the pipeline prints a short confirmation message that includes:
- the repository name,
- and the detected project type.

This gives a quick summary in the Jenkins logs of what path was actually executed.

---

## Stage 2: Run AI agent

## Purpose

The second stage is responsible for preparing the minimal metadata needed by the agent and then executing the external CodeGuardian repository against the pull request context.

Even though the stage name still says `Run AI agent`, the role of Jenkins here is still orchestration. Jenkins does not generate suggestions by itself; it only prepares the environment and launches the agent process.

## Credentials

The stage uses `withCredentials` to inject the required secrets:

- `SONARQUBE_AUTH_TOKEN`
- `LLM_AUTH_TOKEN`
- `BITBUCKET_EMAIL`
- `BITBUCKET_API_TOKEN`
- `ATLASSIAN_MCP_AUTH_HEADER`

This allows the agent to:
- connect to SonarQube,
- access the external generation service,
- authenticate against Bitbucket REST,
- and open the Atlassian Rovo MCP session.

## Pull request metadata

Inside the script block, the pipeline creates a JSON structure containing:

- `pr_id`
- `project_key`
- `repo_slug`
- `workspace`

That structure is written into `data.json`.

This file acts as the input contract between Jenkins and `agent.py`. It is small, explicit and easy to inspect when debugging the pipeline.

## Agent repository cloning

The pipeline does not assume that the agent code lives inside the reviewed repository. Instead, it clones the external agent repository defined by `AGENT_REPO_URL` and checks out the branch defined in `AGENT_REPO_REF`.

This design has two advantages:

1. the reviewed repositories remain clean and do not need to include the agent code,
2. the agent can evolve independently from the repositories it reviews.

The clone is performed quietly and authenticated through the Bitbucket API token.

## Agent execution

Once the repository is cloned and `data.json` is prepared, Jenkins runs:

- `python3 -u AIagent/agent.py --file data.json`

At that point, the pipeline transfers control to the Python agent.

From there, the agent is responsible for:
- retrieving SonarQube issues,
- enriching them with context,
- grouping findings by scope,
- generating proposals,
- validating them,
- and synchronizing comments back into Bitbucket.

---

## Post Actions

The pipeline defines a simple `post { always { ... } }` block.

Its job is to remove temporary artefacts regardless of whether the build succeeded or failed:

- `AIagent`
- `data.json`
- `build.log`
- `sonar-scanner.log`

This cleanup step is useful because it keeps the Jenkins workspace cleaner between executions and reduces the chance of confusing leftovers from previous runs.

---

## Design Decisions

## Pull request only execution

The use of `changeRequest()` in both stages makes the pipeline explicitly focused on pull request review. This is aligned with the purpose of CodeGuardian and avoids mixing review logic with other CI scenarios such as push builds or scheduled jobs.

## Generic project detection

The pipeline does not hardcode repository identities or technology stacks. Instead, it infers the project type from common build files. This makes the solution more reusable across repositories.

## Externalized agent repository

Keeping the agent in a separate repository is a practical decision. It allows the review logic to be updated independently without requiring changes in every analysed repository.

## Explicit failure for incomplete C/C++ analysis

The C and C++ path fails when `compile_commands.json` is missing. This is better than running an incomplete analysis and pretending the result is trustworthy.

## Minimal contract with the agent

The `data.json` file is deliberately small. Jenkins only passes what the agent really needs:
- pull request ID,
- project key,
- repository slug,
- workspace.

This keeps the integration between pipeline and agent simple.

---

## Strengths of the Current Pipeline

The current pipeline has several practical strengths:

- it is easy to follow,
- it is focused on pull requests,
- it adapts to multiple project types,
- it keeps secrets inside Jenkins credentials,
- it isolates the agent from the reviewed repository,
- and it keeps the workspace clean after execution.

From a project point of view, it also fits well with the general philosophy of CodeGuardian: Jenkins orchestrates, SonarQube detects, and the Python agent handles the review logic.

---

## Current Limitations

The current pipeline is functional and clean, but it still has some limitations:

- it does not run unit tests as part of the review flow,
- it does not perform compilation-based validation of generated fixes after the agent stage,
- the agent repository branch is fixed through an environment variable,
- and some project types are analysed with a generic fallback path.

These limitations are acceptable for the current project stage, especially because the pipeline already provides the correct integration points for future extensions.

---

## Possible Future Improvements

Some realistic future improvements for the pipeline would be:

- adding optional test execution before or after the agent stage,
- adding ecosystem-specific validation after fix generation,
- externalizing more configuration per repository,
- improving project type detection for more build systems,
- and storing structured execution artefacts for later evaluation.

These would improve robustness, but they are not required for the current version to be functional.

---

## Conclusion

The Jenkins pipeline is designed as a practical orchestration layer for CodeGuardian. It keeps the execution flow simple while still supporting multiple project types and integrating the main external systems involved in the review process.

Its main contribution is not implementing review logic itself, but coordinating all the necessary steps so that the right repository context, static analysis results and pull request metadata reach the Python agent in a controlled way.

In that sense, the pipeline is a key part of the project because it turns the agent from a standalone script into an automated review component that can run inside a real CI/CD workflow.

---

## Execution Flow Diagram

```mermaid
flowchart TD
    A[Pull request build triggered] --> B[Analyze PR stage]
    B --> C[Checkout SCM]
    C --> D[Extract repository name and project key]
    D --> E[Detect project type]

    E --> F1[Maven build and SonarQube scan]
    E --> F2[Gradle build and SonarQube scan]
    E --> F3[Node SonarQube scan]
    E --> F4[Python SonarQube scan]
    E --> F5[C or C++ SonarQube scan with compile_commands.json]
    E --> F6[Generic SonarQube scan]
    E --> F7[Fail if C or C++ project has no compile_commands.json]

    F1 --> G[Run agent stage]
    F2 --> G
    F3 --> G
    F4 --> G
    F5 --> G
    F6 --> G

    G --> H[Load Jenkins credentials]
    H --> I[Create data.json with PR metadata]
    I --> J[Clone external CodeGuardian agent repository]
    J --> K[Run agent.py]
    K --> L[Agent fetches SonarQube findings]
    L --> M[Agent generates and validates suggestions]
    M --> N[Agent synchronizes inline comments in Bitbucket]
    N --> O[Post action cleanup]