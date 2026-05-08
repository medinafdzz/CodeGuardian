# Metrics and Observability

## Introduction

CodeGuardian is not only designed to generate review suggestions, but also to make its own execution visible and measurable. This is important for two reasons. First, it helps during development and debugging, because it becomes easier to understand how the agent behaves in real executions. Second, it makes the system easier to evaluate as an engineering project, since runtime, token usage and execution patterns can be tracked over time.

For this reason, the current version of the agent exports a small but useful set of metrics to Prometheus through Pushgateway. In addition to that, the agent also logs several execution summaries directly to the Jenkins console.

This document explains what is measured, how those metrics are produced, and why they matter.

---

## Why Metrics Matter in CodeGuardian

The agent depends on multiple external systems and performs several stages in sequence:

- SonarQube issue retrieval,
- scope detection and batching,
- generation of fixes,
- validation of generated proposals,
- synchronization of inline comments.

Because of this, a successful execution is not only about whether the job ends without crashing. It is also important to understand:

- how long the analysis takes,
- how many tokens are consumed,
- whether cache reuse is working,
- how many generated issues survive validation,
- and how many comments are finally created, reused or deleted.

Without this information, the system would be harder to evaluate and harder to improve.

---

## Current Observability Model

The current observability model is based on two sources:

1. **Prometheus metrics pushed by the agent**
2. **execution summaries written to the logs**

This means the system supports both:
- machine-readable monitoring through Prometheus and Grafana,
- and human-readable inspection through Jenkins logs.

The metric values are collected across the analysis, validation and synchronization stages. The final push to Pushgateway is centralized in `codeguardian/metrics.py`.

---

## Prometheus Integration

The agent uses the `prometheus_client` library and pushes data to **Prometheus Pushgateway**. The relevant imports are:

- `CollectorRegistry`
- `Gauge`
- `push_to_gateway`

The metrics are pushed near the end of the agent execution, once the model interaction, validation and Bitbucket synchronization have completed.

### Why Pushgateway is used

The agent is executed inside Jenkins as a short-lived process. Because of that, it does not expose a long-running HTTP metrics endpoint. A pull-based Prometheus scrape model would not fit well here.

Pushgateway solves that problem by allowing the agent to push its metrics at the end of each execution. This matches the execution model of CodeGuardian much better.

---

## Metrics Collected

The current implementation defines several metric families.

### 1. `codeguardian_analysis_latency_seconds`

This metric stores the total response time of the AI analysis stage, measured in seconds. It is created as a `Gauge` and is set using the difference between the current time and the start time recorded before batch processing begins.

### Why it matters

This metric is useful to understand how expensive the generation stage is in practice. If latency increases significantly over time, possible causes include:

- more findings per pull request,
- weaker cache reuse,
- slower model response,
- or general infrastructure issues.

---

### 2. `codeguardian_last_execution_timestamp`

This metric stores the timestamp of the last execution. It is also a `Gauge`, and it is set with the current Unix timestamp.

### Why it matters

This metric is useful for operational visibility. It helps answer simple questions such as:
- when was the last successful agent execution,
- whether the job is still active,
- and whether a repository has stopped receiving analysis updates.

---

### 3. `codeguardian_analysis_prompt_tokens`

This metric stores the number of prompt tokens used during the AI analysis phase. The value is accumulated across batch executions.

### Why it matters

Prompt token count is one of the main indicators of request size and model input cost. It is helpful when:
- comparing different prompt designs,
- tuning the number of SonarQube issues sent to the model,
- or evaluating the cost impact of larger scope-based batches.

---

### 4. `codeguardian_analysis_response_tokens`

This metric stores the number of tokens generated in the model response, also accumulated across the full analysis step.

### Why it matters

Response token count helps understand how large the model outputs are in practice. It is also useful when comparing:
- single-issue vs grouped-issue outputs,
- stricter vs looser prompt strategies,
- and different model configurations.

---

### 5. `codeguardian_analysis_total_tokens`

This metric stores the total number of tokens used during the AI analysis phase. It is the broadest token metric and combines the overall token consumption of the interaction.

### Why it matters

This is the most direct metric for measuring model usage at execution level. It is especially useful for:
- cost estimation,
- execution trend analysis,
- and evaluating whether cache improvements are reducing repeated token consumption.

---

### 6. Cache metrics

The agent exports:

- `codeguardian_analysis_cached_tokens`
- `codeguardian_batch_cache_hits_total`
- `codeguardian_batch_cache_misses_total`

These metrics show how much cache reuse happened during an execution. This is useful because cache behaviour has a direct effect on latency and token usage.

---

### 7. Issue flow metrics

The agent exports:

- `codeguardian_sonar_findings_total`
- `codeguardian_generated_issues_total`
- `codeguardian_invalid_issues_total`
- `codeguardian_patch_invalid_issues_total`
- `codeguardian_final_issues_total`
- `codeguardian_blocking_findings`

These values make the validation effect visible in Prometheus. They show how many findings entered the agent, how many suggestions were generated, how many were discarded and how many survived.

---

### 8. Bitbucket comment metrics

The agent exports:

- `codeguardian_comments_desired_total`
- `codeguardian_comments_created_total`
- `codeguardian_comments_reused_total`
- `codeguardian_comments_deleted_total`

These metrics show how the inline comment synchronization behaved in the pull request.

---

## How Metrics Are Calculated

The AI-related metric calculation happens inside `analyze_code_with_gemini()`. The final registry creation and Pushgateway export happen inside `codeguardian/metrics.py`.

### Latency timing

At the beginning of the function, the agent stores:

- `start_time = time.time()`

Later, once all AI batches have been processed, it computes:

- `duration = time.time() - start_time`

That value is then written into the latency metric.

### Token accumulation

The function keeps four accumulators:

- `total_prompt_tokens`
- `total_response_tokens`
- `total_tokens`
- `total_cached_tokens`

Whenever a real model call is made, the code tries to read usage metadata from the response object and adds the values to those accumulators. This means the final metrics reflect the full batch analysis of the current pull request, not just one individual request.

### Registry creation

The code uses a dedicated `CollectorRegistry()` instead of the global default registry. This is a good design choice because it keeps the metrics for each execution isolated and avoids polluting a shared process-wide registry.

---

## Grouping Labels Used in Pushgateway

When the metrics are pushed, the agent includes a grouping key with the following labels:

- `build_number`
- `event_type`
- `display_id`
- `repository`
- `exec_timestamp`

### Meaning of each label

#### `build_number`
This is usually taken from Jenkins `BUILD_NUMBER`. It identifies the specific pipeline run.

#### `event_type`
In the current version it is set to `"pull_request"`, which matches the actual purpose of the pipeline.

#### `display_id`
This is a formatted label combining repository name and build number. It gives a human-readable identifier for dashboards.

#### `repository`
This stores the SonarQube project key or repository identity associated with the run.

#### `exec_timestamp`
This stores the execution time as an integer string, allowing each push to be associated with a specific run moment.

### Why these labels are useful

These labels make it easier to build Grafana dashboards and to filter metrics by:
- repository,
- build,
- or execution time.

They also make historical comparisons easier when analyzing multiple runs.

---

## Cache-Related Observability

Batch cache hits and misses are exported as Prometheus metrics and are also recorded in the logs.

At the end of the AI analysis step, the agent logs:

- number of produced issues,
- total cached tokens, when available,
- batch cache hits,
- batch cache misses.

### Why this matters

These log values are useful because cache behaviour strongly influences:
- latency,
- token usage,
- and overall cost.

For example:
- a cold run with many cache misses will normally be slower and consume more tokens,
- while a warm run with reused cached results should be faster and cheaper.

This data is useful in dashboards and also in Jenkins logs when debugging one specific execution.

---

## Validation and Execution Summaries in Logs

The agent also logs several summaries outside the Prometheus section.

### Patch validation summary

After AI returns its issues and the normalization step is applied, the agent validates the proposed patches. Invalid issues are dropped and logged individually with the reason.

Then, the execution summary includes:

- total SonarQube findings,
- generated issues,
- invalid issues dropped,
- issues dropped after patch validation,
- final surviving issues,
- whether blocking findings were present.

This summary is very useful for experimental evaluation because it shows how the candidate suggestions are reduced before final publication.

### Inline synchronization summary

At the end of the Bitbucket synchronization stage, the agent logs:

- desired comments,
- created comments,
- reused comments,
- deleted comments.

This makes it possible to understand how much of the review state was:
- already up to date,
- newly created,
- or obsolete and removed.

---

## Relationship Between Metrics and Logs

The current design intentionally uses both metrics and logs because they serve different purposes.

### Metrics are better for:
- trend monitoring,
- dashboards,
- long-term repository comparison,
- cost and latency visualization.

### Logs are better for:
- execution debugging,
- understanding why suggestions were dropped,
- checking cache behaviour in a specific run,
- and tracing synchronization decisions.

In that sense, Prometheus metrics and Jenkins logs complement each other rather than replacing one another.

---

## Practical Use Cases

The current metrics and logs already support several useful scenarios.

### 1. Performance tracking

By looking at latency and token metrics, it is possible to understand whether the analysis stage is becoming slower or more expensive over time.

### 2. Cache evaluation

By looking at logged batch cache hits and misses, it is possible to evaluate whether the current batching and cache signature strategy are working as expected.

### 3. Reliability evaluation

By comparing:
- generated issues,
- dropped issues,
- and final issues,

it is possible to measure how strict the validation layer is in practice.

### 4. Pull request reporting behaviour

By reading the synchronization summary, it becomes possible to see whether most comments are being reused or whether the system is constantly recreating them.

---

## Current Limitations

The current observability model is useful, but it still has limitations.

### 1. Some useful data remains only in logs

Most execution-level values are now exported as Prometheus metrics. Some lower-level details, such as individual rejection reasons or per-batch failures, are still available only in logs.

### 2. No per-batch metrics

The current design records analysis metrics for the whole execution, not for each individual batch. This keeps the implementation simpler, but it limits fine-grained investigation.

### 3. No metric for external system failures

Failures in SonarQube, Bitbucket REST or MCP are logged, but there are no dedicated Prometheus counters for those error categories.

### 4. No repository-level historical aggregation inside the agent

The agent pushes execution-level metrics, but it does not implement its own historical storage or aggregated statistics. That responsibility is left to Prometheus and Grafana.

---

## Possible Future Improvements

Several extensions could make the metrics system stronger in later iterations:

- adding counters for REST failures and MCP failures,
- adding repository-level quality indicators,
- exporting per-batch latency and token usage,
- and adding long-term aggregate panels by repository.

These improvements would be especially useful in a more production-oriented deployment.

---

## Why the Current Design Is Still Valuable

Even though the current metrics are not exhaustive, they already provide a good monitoring baseline for the project.

The system measures the most important execution costs:
- runtime,
- token consumption,
- and last execution time.

At the same time, the logs provide enough visibility into:
- validation filtering,
- cache reuse,
- and comment synchronization.

For a short-lived Jenkins-based agent, this is already a useful and practical observability model.

---

## Conclusion

The metrics and observability layer of CodeGuardian helps turn the agent into something that can be evaluated and monitored, not just executed.

By exporting latency and token usage to Prometheus and by logging detailed execution summaries, the system provides enough visibility to study:
- performance,
- cost,
- cache effectiveness,
- validation behaviour,
- and Bitbucket synchronization results.

This makes the project easier to debug, easier to justify in a technical report, and easier to extend in future iterations.
