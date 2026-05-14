# CodeGuardian Architecture

## Overview

CodeGuardian is an automated pull request review assistant designed to run inside a CI/CD pipeline. Its main goal is to analyze the code introduced in a pull request, identify relevant issues from static analysis, generate safe fix proposals with the help of a large language model, and publish those proposals directly as inline comments in Bitbucket.

The system was designed as a practical and reusable solution rather than as a language-specific prototype. For that reason, the architecture tries to keep the workflow generic enough to support different repositories and different technology stacks with minimal repository-specific changes.

At a high level, the process starts when a pull request triggers Jenkins. Jenkins checks out the repository under review, runs static analysis with SonarQube, and then executes the AI agent. The agent collects the relevant findings, groups them by code scope, asks the language model for possible fixes, validates the generated patches, and finally synchronizes the results back into the pull request as inline comments. The optional optimization review can add extra comments after the same validation barrier by inspecting changed functions, methods and selected build/configuration files for clear runtime, build-time, IO, network, memory or algorithmic improvements.

---

## General Architecture

The architecture is based on five main building blocks:

1. **Bitbucket**, which hosts the source code and the pull requests.
2. **Jenkins**, which orchestrates the pipeline execution.
3. **SonarQube**, which provides the static analysis findings.
4. **The CodeGuardian agent**, implemented in Python, which contains the orchestration and decision logic.
5. **AI**, which generates fix proposals from the selected findings and optional optimization suggestions from explicit candidates.

In addition to these main elements, the system also uses:
- **Atlassian Rovo MCP**, to read pull request comments from Bitbucket.
- **Bitbucket REST API**, to create and delete inline comments reliably.
- **Prometheus Pushgateway**, to export execution metrics from the agent.

This combination allows the system to separate responsibilities clearly: SonarQube is responsible for detection, the agent is responsible for filtering and validation, the LLM is responsible for proposing changes, and Bitbucket is the place where the final review feedback is published.

---

## End-to-End Flow

The full execution flow can be summarized as follows:

### 1. Pull request event

A pull request event triggers the Jenkins pipeline. The pipeline checks out the pull request workspace, ensuring the analysis is performed on the effective state of the code being reviewed.

### 2. Repository analysis

Jenkins runs SonarQube analysis on the repository under review. The objective here is not to analyze the entire codebase again from scratch, but to focus on relevant findings related to the code introduced or modified in the pull request.

### 3. Retrieval of SonarQube issues

The Python agent connects to SonarQube through the MCP server and retrieves unresolved issues for the target project. The code then filters, orders and enriches those findings before sending anything to the language model. In the current implementation, the agent keeps only relevant severities, limits the total number of findings, and enriches each one with code context and scope information.

### 4. Scope resolution and batching

One of the key architectural decisions in CodeGuardian is that findings are not handled only by line proximity. Instead, the agent tries to resolve the surrounding scope of each finding, such as a function or method, and then groups findings accordingly. This makes the generated suggestions more coherent, because the model can reason about a complete scope instead of isolated lines. The current agent includes scope detection logic for Python and several brace-based languages such as Java, JavaScript, TypeScript, Go, C#, C/C++, PHP, Ruby, Rust, Kotlin and Swift.

### 5. Generation of fixes with AI

After batching, the agent sends the selected findings to the AI model. The prompt is strongly constrained: the model must return valid JSON, keep the original SonarQube key, propose only real code modifications, preserve concrete types when needed, avoid unsafe shorthand refactors, and return no issue at all if the replacement is not safe enough. The current implementation also supports prompt caching and batch-level caching to reduce repeated requests and lower execution cost.

When optimization review is enabled, the agent uses the pull request diff to collect changed function or method scopes and selected build/configuration files. Gemini receives one candidate at a time and must justify a clear improvement with current and proposed time/space or build/runtime cost estimates. The output is still normalized, validated and synchronized through the same publication pipeline as SonarQube-backed issues.

### 6. Validation of generated patches

A second important architectural decision is that model output is never published blindly. Once the agent receives the proposed issues, it normalizes them and validates them before sending anything back to Bitbucket. The validation step checks whether the `original_code` proposed by the model actually matches the current file content in the repository. If the block does not match, the issue is discarded. For Python files, the patched result is also parsed with `ast.parse()` as a basic syntax check.

### 7. Synchronization of pull request comments

If a proposal passes validation, the agent prepares the final inline comment content and synchronizes it with the pull request. Instead of deleting and recreating everything on each run, CodeGuardian reads existing inline comments, identifies the ones created by the agent, compares them against the desired state, deletes obsolete comments, reuses matching ones, and only creates the missing comments. The current signature used for synchronization includes file path, target line, issue identifiers and a content hash, which makes the synchronization more precise than a simple line-based comparison.

### 8. Metrics export

At the end of the AI analysis step, the agent exports execution metrics such as latency and token usage to Prometheus Pushgateway. This makes it possible to monitor the cost and runtime behaviour of the system across builds.

---

## Main Components

## Jenkins

Jenkins acts as the orchestration layer. It is responsible for:
- receiving the pull request trigger,
- checking out the repository,
- running static analysis,
- preparing the input data for the agent,
- and launching the Python agent in a controlled environment.

In this architecture, Jenkins is not responsible for making review decisions. Its role is to coordinate the pipeline and provide the execution context.

## SonarQube

SonarQube is the primary source of findings. It provides the initial static analysis signal that determines what the AI agent should inspect. This is an important design decision, because it prevents the language model from scanning the whole repository without guidance. Instead, the model works on a filtered and structured subset of issues.

## Python Agent

The Python agent is the core of the system. It is responsible for:
- retrieving SonarQube findings,
- enriching them with context,
- resolving scope,
- grouping issues,
- calling AI,
- validating generated patches,
- synchronizing comments with Bitbucket,
- and exporting metrics.

This means the agent is the real decision layer of the architecture. It connects all the other components and adds the control logic needed to make the workflow reliable.

## AI

AI is used as the proposal engine, not as the only source of truth. For defect fixing, the model receives findings already detected by SonarQube and is asked to suggest small, concrete replacements. For optimization review, it receives changed candidates and must provide a direct replacement backed by explicit runtime, build-time, IO, network, memory or algorithmic reasoning. This keeps the architecture grounded while avoiding broad repository-wide LLM review.

## Bitbucket

Bitbucket is both the source repository and the final interaction point for developers. The generated feedback appears directly in the pull request as inline comments, which makes the system easier to integrate into an existing review workflow.

---

## Architectural Decisions

## 1. Detection and generation are separated

The system separates issue detection from fix generation. SonarQube detects issues, while the AI model only proposes possible fixes for those issues. This reduces the search space for the model and improves control over the output.

## 2. Scope-based grouping instead of simple proximity

A major decision was to group findings by code scope whenever possible. This produces more coherent suggestions and reduces duplicated or fragmented comments.

## 3. Validation before publication

The architecture does not trust model output by default. Suggestions are validated against the actual file content before they are published. This is one of the main reliability mechanisms in the system.

## 4. Read via MCP, write via REST

Another practical decision is the split between reading and writing in Bitbucket integration. Atlassian Rovo MCP is used to read pull request comments, while Bitbucket REST is used to create and delete inline comments. This mixed approach was chosen because it is more reliable for the current use case than relying on a single integration path for everything.

## 5. Incremental synchronization instead of full recreation

The agent does not recreate all comments on every execution. Instead, it compares the current pull request state with the desired one and only applies the necessary changes. This reduces noise in the review and avoids unnecessary churn.

## 6. Caching for efficiency

The architecture includes both prompt cache metadata and batch cache storage. This reduces repeated calls to the model when the same or very similar findings are processed again. The batch signature also includes a hash of the real scope content, which makes the cache safer against stale reuse.

## 7. Optimization review is changed-scope and validation-gated

Optional optimization comments are generated only from changed function/method scopes or selected changed build/configuration files. Batch signatures include the review type, model, optimization rules hash, file path, scope identity and scope content hash, which keeps them separate from SonarQube review cache entries. The feature is best-effort and does not replace benchmarks, profiling, compilation or tests.

---

## Reliability Considerations

Even though the system is already functional, the architecture does not assume that every model-generated patch is correct. For this reason, several safety barriers are included:

- strict prompt constraints,
- normalization and deduplication of generated issues,
- file-content matching before patch acceptance,
- syntax validation for Python,
- and controlled synchronization of comments.

These measures do not guarantee perfect correctness, but they significantly reduce the probability of publishing invalid suggestions.

---

## Current Limitations

The current architecture still has some limitations:

- syntax validation is stronger for Python than for other ecosystems,
- generated fixes are not compiled or tested before publication,
- the system still depends on the quality of SonarQube findings,
- performance candidate detection is based on changed function or method scopes and the existing scope parser,
- and some languages rely on heuristic scope detection instead of full parsing.

These limitations are acceptable for the current project stage, especially because the architecture already includes the right control points for future extensions.

---

## Future Evolution

This architecture leaves room for several future improvements:

- ecosystem-specific validation, such as Maven or Gradle compilation,
- export of structured results in formats such as JSON or SARIF,
- project-specific review profiles,
- richer performance candidate collection for languages and cases that need deeper parsing,
- richer dashboards based on the exported metrics,
- and integration of additional static analysis tools as complementary sources of findings.

The important point is that these improvements can be added without redesigning the whole system, because the current architecture already separates orchestration, detection, generation, validation and publication.

---

## Conclusion

CodeGuardian has been designed as a practical architecture for automated pull request review in a CI/CD context. Its main strength is not only that it generates code suggestions, but that it does so inside a controlled workflow where findings are first detected by static analysis, then enriched and grouped by the agent, then processed by the language model, and finally validated before publication.

From an architectural point of view, this makes the system more robust than a simple AI assistant attached to a repository. It behaves as an orchestrated review component integrated into the development pipeline, which is closer to what would be expected in a real industrial environment.
