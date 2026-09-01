# CodeGuardian Demo Script

## Goal

This demo shows CodeGuardian as a controlled pull request review assistant. The key message is that the system does not publish raw LLM output. It combines SonarQube, AI proposal generation, patch validation, Bitbucket synchronization and observability.

The demo can be run against a controlled sample repository or against a real industrial-style repository, as long as the Jenkins, SonarQube and Bitbucket context is available.

---

## Demo Setup

Before the demo, prepare:

- Jenkins running the CodeGuardian pipeline.
- SonarQube available and configured for the target repository.
- Bitbucket credentials and pull request access.
- Prometheus, Pushgateway and Grafana running from the infrastructure repository.
- `CODEGUARDIAN_ENABLE_OPTIMIZATION_REVIEW=true` if the optimization review part will be shown.

Recommended performance configuration:

```text
CODEGUARDIAN_DEMO_FAST_MODE=true
CODEGUARDIAN_ENABLE_OPTIMIZATION_REVIEW=true
CODEGUARDIAN_MAX_OPTIMIZATION_SCOPES=5
CODEGUARDIAN_OPTIMIZATION_ONLY_CHANGED_FILES=true
CODEGUARDIAN_OPTIMIZATION_BATCH_SIZE=3
CODEGUARDIAN_SKIP_OPTIMIZATION_FOR_CONFIG_FILES=true
CODEGUARDIAN_OPTIMIZATION_REQUIRE_CLEAR_GAIN=true
```

For the shortest demo run, set `CODEGUARDIAN_ENABLE_OPTIMIZATION_REVIEW=false`. SonarQube-backed review, Bitbucket synchronization, result export and metrics still run.

---

## Scenario 1 - SonarQube-Backed Code Issue

### Purpose

Show the main CodeGuardian workflow: SonarQube detects a real issue, Gemini proposes a fix, CodeGuardian validates it and Bitbucket receives an inline comment.

### Suggested Change

Create or modify a pull request with a clear static analysis issue, for example:

- duplicated condition,
- null handling issue,
- unused or unsafe branch,
- simple bug detected by SonarQube.

### What To Show

1. Open the pull request in Bitbucket.
2. Show the Jenkins build triggered by the pull request.
3. Show that SonarQube analysis runs before the agent.
4. Open the CodeGuardian logs and point out:
   - SonarQube findings retrieved,
   - AI analysis started,
   - validation summary,
   - Bitbucket synchronization summary.
5. Open the Bitbucket PR comments.
6. Show the generated `Code Issue` inline comment.

### Message To Explain

CodeGuardian does not ask the LLM to inspect the whole repository freely. SonarQube provides the initial signal, and the agent validates that the proposed replacement matches the real file before publishing.

---

## Scenario 2 - Optimization Review

### Purpose

Show that CodeGuardian can also publish non-blocking optimization suggestions when a changed function, method or build/configuration file has a clear runtime, build-time, IO, network, memory or algorithmic improvement.

### Suggested Change

Add or modify one changed function with repeated linear lookup, for example:

```python
def find_matches(users, allowed_ids):
    result = []
    for user in users:
        if user.id in allowed_ids:
            result.append(user)
    return result
```

Possible performance comment:

```text
CodeGuardian optimization suggestion

Performance issue:
The function performs a membership check against a list for every user.

Current estimated complexity: O(n*m)
Proposed estimated complexity: O(n+m)

Complexity justification:
Building a set once makes membership checks average O(1), avoiding repeated linear scans.
```

### What To Show

1. Show that optimization review is enabled through environment variables.
2. Show the changed function in the pull request.
3. Show Jenkins logs with the number of performance candidate scopes.
4. Show the final `CodeGuardian optimization suggestion` comment in Bitbucket.
5. Explain that this mode is best-effort and does not replace profiling, benchmarks or tests.

### Message To Explain

The optimization flow is complementary to SonarQube. It only reviews changed candidates and publishes a comment when the model can justify a direct replacement with better estimated runtime, build-time or complexity cost.

---

## Scenario 3 - Metrics and Dashboard

### Purpose

Show that the system is observable and measurable.

### What To Show

Open Grafana and show:

- analysis latency,
- token usage,
- issue flow,
- Bitbucket comment synchronization,
- batch cache hits and misses,
- performance candidate and suggestion counts.

If time series look continuous, explain that Grafana uses the latest known values because CodeGuardian is a batch job executed by Jenkins, not a long-running service.

### Message To Explain

The TFG can be evaluated not only by looking at comments, but also by measuring execution cost, token usage, cache behaviour, validation effect and synchronization behaviour.

---

## Scenario 4 - Limitations and Future Work

### Current Limitations

- Optimization review is best-effort and based on changed function/method scopes and selected build/configuration files.
- Scope detection for brace-based languages is heuristic.
- Validation checks applicability and Python syntax, but does not prove semantic correctness.
- Full external integration tests still depend on Jenkins, Bitbucket, SonarQube and credentials.

### Future Work

- Improve performance candidate collection with deeper language parsing.
- Add ecosystem-specific compile or test validation.
- Add repository-level configuration with a `.codeguardian.yml` file.
- Add per-language performance examples for demos.

### Message To Explain

The current version is a complete prototype with clear extension points. It demonstrates the architecture and the workflow, while leaving well-defined future work for a production-grade system.

---

## Closing Demo Narrative

The final presentation can be summarized as:

1. SonarQube detects defects.
2. CodeGuardian prepares context and calls the LLM in a controlled way.
3. Generated replacements are validated before publication.
4. Optional optimization review can add runtime, build-time or algorithmic suggestions for changed scopes.
5. Bitbucket comments are synchronized incrementally.
6. Metrics make the process observable in Grafana.

This supports the main TFG claim: CodeGuardian is not just a chatbot attached to a repository, but an automated pull request review component integrated into a CI/CD workflow.
