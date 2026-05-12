# CodeGuardian Demo Script

## Goal

This demo shows CodeGuardian as a controlled pull request review assistant. The key message is that the system does not publish raw LLM output. It combines static analysis, candidate detection, AI proposal generation, patch validation, Bitbucket synchronization and observability.

The demo can be run against a controlled sample repository or against a real industrial-style repository such as ESS, as long as the Jenkins, SonarQube and Bitbucket context is available.

---

## Demo Setup

Before the demo, prepare:

- Jenkins running the CodeGuardian pipeline.
- SonarQube available and configured for the target repository.
- Bitbucket credentials and pull request access.
- Prometheus, Pushgateway and Grafana running from the infrastructure repository.
- `CODEGUARDIAN_ENABLE_IMPROVEMENTS=true` if the improvement review part will be shown.

Recommended improvement configuration:

```text
CODEGUARDIAN_ENABLE_IMPROVEMENTS=true
CODEGUARDIAN_MAX_IMPROVEMENTS=3
CODEGUARDIAN_MAX_IMPROVEMENT_CANDIDATES=10
CODEGUARDIAN_MAX_IMPROVEMENT_FILES=4
CODEGUARDIAN_MAX_IMPROVEMENT_CHARS=18000
CODEGUARDIAN_IMPROVEMENT_EXCLUDE=generated/,release/,target/,build/,gnat/,essFramework/release/
```

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

## Scenario 2 - Code Improvement Review

### Purpose

Show that CodeGuardian can also suggest non-blocking maintainability improvements, separate from SonarQube defects.

### Code Change

Add or modify one changed file with a small maintainability issue. For example, a broad Java exception handler:

```java
int loadValue(String rawValue) {
    try {
        return Integer.parseInt(rawValue);
    } catch (Exception error) {
        return 0;
    }
}
```

Expected candidate:

```text
category=error_handling
evidence=broad_exception=Exception
```

Possible comment:

```text
Code Improvement

Improvement opportunity:
This handler catches every Exception, which can hide unrelated failures and make diagnosis harder.

Suggested improvement:
Catch the expected conversion error or keep the original exception context.
```

Other valid examples are console prints in Java, fragile conditions in scripts, or basic C/C++ maintainability signals:

```bash
if [ -d $ESS_HOME ] ; then
  echo ready
fi
```

Expected candidate:

```text
category=resource_handling
evidence=unquoted_test_variable=ESS_HOME
```

Possible comment:

```text
Code Improvement

Improvement opportunity:
The path variable is used without quotes in a shell test. If it is empty or contains spaces, the condition can fail unexpectedly.

Suggested improvement:
Quote the variable in the test expression.
```

### What To Show

1. Show that improvement review is enabled through environment variables.
2. Show the changed file in the pull request.
3. Show the Jenkins log line with detected static improvement candidates.
4. Show the final `Code Improvement` comment in Bitbucket.
5. Explain that these comments are non-blocking and separate from SonarQube-backed issues.

### Message To Explain

The improvement flow is candidate-driven. CodeGuardian first detects maintainability signals in changed files, then sends those candidates to the model. The LLM is used to produce a clear review suggestion, not to invent arbitrary improvements from scratch.

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
- improvement candidate count.

If time series look continuous, explain that Grafana uses the latest known values because CodeGuardian is a batch job executed by Jenkins, not a long-running service.

### Message To Explain

The TFG can be evaluated not only by looking at comments, but also by measuring execution cost, token usage, cache behaviour, validation effect and synchronization behaviour.

---

## Scenario 4 - Limitations and Future Work

### Current Limitations

- Improvement detection currently covers Python, shell/KSH, Java and basic C/C++ signals.
- Candidates are detected in changed files, but not yet strictly filtered by changed line ranges.
- Improvement responses are guided by candidates, but the response does not yet carry a strict `candidate_id`.
- Validation checks applicability and Python syntax, but does not prove semantic correctness.
- Full external integration tests still depend on Jenkins, Bitbucket, SonarQube and credentials.

### Future Work

- Filter improvement candidates by changed line ranges.
- Add `candidate_id` to the prompt and validate responses against it.
- Add XML, duplication and deeper performance detectors.
- Integrate external tools such as Ruff, ShellCheck, Semgrep or CPD as additional candidate sources.
- Add metrics by improvement category.
- Add repository-level configuration with a `.codeguardian.yml` file.

### Message To Explain

The current version is a complete prototype with clear extension points. It demonstrates the architecture and the workflow, while leaving well-defined future work for a production-grade system.

---

## Closing Demo Narrative

The final presentation can be summarized as:

1. SonarQube detects defects.
2. CodeGuardian prepares context and calls the LLM in a controlled way.
3. Generated replacements are validated before publication.
4. Optional improvements are based on static candidates, not only LLM opinion.
5. Bitbucket comments are synchronized incrementally.
6. Metrics make the process observable in Grafana.

This supports the main TFG claim: CodeGuardian is not just a chatbot attached to a repository, but an automated pull request review component integrated into a CI/CD workflow.
