# Validation Strategy

## Introduction

One of the main risks in an AI-assisted code review system is publishing suggestions that look reasonable at first sight but do not actually fit the current source code, do not compile, or do not even correspond to the lines that triggered the finding. Because of that, validation became a necessary part of CodeGuardian instead of an optional improvement.

The purpose of the validation stage is not to prove that every generated fix is semantically perfect. That would require much stronger project-specific checks, such as compilation, testing, or deeper language-aware analysis. Instead, the goal of the current validation strategy is to reject suggestions that are clearly unsafe, inconsistent, or no longer applicable to the real file contents.

In practical terms, validation acts as a safety barrier between the language model output and the final publication of inline comments in Bitbucket.

---

## Why Validation Is Necessary

A prompt alone is not enough to guarantee correct code changes. Even when the model is strongly instructed to return safe and minimal replacements, it may still produce proposals that:

- do not exactly match the current source code,
- target the wrong code block,
- use a replacement based on stale context,
- or introduce syntax errors.

This means that a system that directly publishes model output would be too fragile for a real CI/CD workflow. For this reason, CodeGuardian validates the generated issues after the AI step and before the Bitbucket reporting step.

---

## Validation Goals

The current validation strategy has four main goals:

1. Ensure that the proposed replacement still matches the current version of the file.
2. Reject malformed or trivial model outputs.
3. Add at least one language-specific syntax barrier where it is inexpensive and reliable.
4. Prevent invalid suggestions from reaching the pull request comments.

These goals are intentionally practical. The system is designed to reduce obvious bad suggestions without turning the validation stage into a full build system or language server.

---

## Position of Validation in the Workflow

Validation happens after AI has returned a list of proposed issues and before those issues are synchronized back to Bitbucket.

The simplified order is:

1. SonarQube findings are retrieved.
2. Findings are grouped and sent to AI.
3. AI returns proposed issues.
4. The agent normalizes the generated issues.
5. The agent validates the issues.
6. Only the surviving issues are turned into inline comments.

This means the model is allowed to propose fixes, but the agent still decides whether those proposals are safe enough to be published.

---

## Validation Stages

The current validation strategy is composed of two main stages:

- normalization and structural filtering,
- patch applicability validation.

In Python, a third stage is added:
- syntax validation with `ast.parse()`.

---

## 1. Normalization and Structural Filtering

The first validation-related step is performed by `normalize_issues()`. This function does not validate the patch against the repository yet, but it removes many obviously unusable outputs before deeper checks happen.

### What this stage checks

`normalize_issues()` performs several operations:

- trims and normalizes string fields,
- normalizes severity values,
- converts escaped replacement text into actual multiline content,
- normalizes `original_code` and `proposed_code`,
- rejects empty code blocks,
- rejects proposals where the original and proposed code are effectively identical,
- normalizes missing or invalid line numbers,
- and removes duplicated issues based on the issue key.

This stage is useful because even if the model returns valid JSON, the content may still be trivial, empty, duplicated, or internally inconsistent.

### Why this matters

Without this step, the agent could still publish comments that contain:

- empty replacements,
- cosmetic no-op changes,
- malformed ranges,
- or duplicated issue proposals.

So, even though this is not yet semantic validation, it is an important first filter that makes the rest of the process more robust.

---

## 2. Patch Applicability Validation

The main validation barrier is based on checking whether the patch proposed by the model can actually be applied to the current file content.

This logic is implemented through two functions:
- `patched_file_content()`
- `validate_issue()`

### `patched_file_content()`

This function reconstructs what the target file would look like if the model-generated patch were applied.

The process is the following:

1. It checks that the file exists.
2. It loads the file contents from disk.
3. It resolves the effective start and end line of the proposed replacement.
4. It extracts the real code block from the file.
5. It normalizes the extracted block and the `original_code` returned by the model.
6. It compares both normalized blocks.
7. If they match, it builds the patched version of the file in memory.
8. If they do not match, it returns `None`.

### Why this check is important

This is the most important safety barrier in the current system. It prevents the agent from publishing a suggestion when:

- the model chose the wrong lines,
- the scope changed,
- the file content is different from what the model assumed,
- or the proposed replacement is based on code that does not actually exist in the repository anymore.

In other words, the model is not trusted just because it returned a plausible code snippet. The proposal must still match the real source file.

---

## 3. Syntax Validation for Python

The current implementation adds one extra validation step for Python files.

If `validate_issue()` detects that the target file is Python, it parses the fully patched content with `ast.parse()`. If parsing fails, the issue is rejected.

### Why only Python

Python was chosen because:
- the standard library already provides a reliable parser through `ast`,
- it does not require project-specific compilation setup,
- and it adds a real extra guarantee at very low implementation cost.

For other ecosystems, equivalent validation is usually more expensive and context-dependent. For example:
- Java validation often requires Maven or Gradle context,
- TypeScript usually depends on a `tsconfig.json`,
- C and C++ may require compiler flags, includes and project-specific build metadata.

For that reason, the current system provides a generic applicability validation for all languages, and an additional syntax barrier only for Python.

### What this guarantees

This step guarantees only that the patched Python file is syntactically valid. It does not guarantee:
- semantic correctness,
- correct runtime behaviour,
- or project-level compatibility.

Still, it is a meaningful improvement because it filters out one more class of obviously invalid suggestions.

---

## Final Validation Function

The main validation entry point for each generated issue is `validate_issue()`.

This function combines the previous logic:

- it first tries to build the patched file content,
- and only if that succeeds, it performs the optional Python syntax check.

Its output is a tuple:
- a boolean indicating whether the issue is valid,
- and a textual reason when validation fails.

This design is useful because it allows the caller to log why the issue was rejected, which improves traceability during debugging and evaluation.

---

## Filtering Invalid Proposals

The function `filter_valid_issues()` applies `validate_issue()` to all generated issues and keeps only the valid ones. Invalid issues are counted and logged with the corresponding reason.

This means validation is not only a local helper; it directly affects the final output of the system.

In practice, after AI returns its proposals, the final list of issues may become smaller because some of them are filtered out during validation. This is expected behaviour. In fact, it is one of the indicators that the system is behaving conservatively instead of blindly trusting the model.

---

## What the Current Validation Prevents

The current strategy helps prevent several common failure cases, including:

- suggestions whose `original_code` does not match the real file content,
- suggestions pointing to invalid or inconsistent line ranges,
- duplicated or empty generated issues,
- no-op replacements where nothing really changes,
- and syntax-breaking Python replacements.

This makes the system more reliable than a pure prompt-based solution, even though it still remains lighter than a full compiler or test-based validation pipeline.

---

## What the Current Validation Does Not Guarantee

It is important to state clearly what this validation strategy does **not** solve.

The current approach does not guarantee:

- semantic correctness of the fix,
- successful compilation for Java, C, C++, or other compiled languages,
- project-level dependency correctness,
- test success,
- or absence of behavioural regressions.

This limitation is intentional. The validation implemented in the current version is designed as a lightweight and generic safety layer, not as a full build-verification framework.

From an academic point of view, this is still valuable because it demonstrates that the system does not rely exclusively on prompt engineering. Instead, it introduces an explicit verification step before publication.

---

## Why This Design Was Chosen

The validation mechanism was designed with three practical constraints in mind:

### 1. It had to be lightweight

The agent runs inside a CI/CD pipeline, so validation could not become too expensive or require a heavy project-specific setup for every issue.

### 2. It had to be generic

The project is intended to support multiple languages and repositories. A validation mechanism that depends too much on one build system or one language would reduce the generality of the solution.

### 3. It had to provide real value

A weak or cosmetic validation step would not justify its complexity. The implemented checks were selected because they reduce real failure cases in a measurable way.

This is why the current solution focuses first on applicability validation and only then adds a language-specific syntax check where it is cheap and reliable.

---

## Relationship with Prompt Constraints

Validation and prompting serve different purposes.

The prompt tries to guide the model towards:
- smaller replacements,
- safe and explicit code,
- correct JSON output,
- and rejection of unsafe fixes.

Validation, on the other hand, does not try to guide. It tries to verify.

This distinction is important. Prompt constraints reduce the probability of bad output, while validation reduces the probability of publishing bad output.

In that sense, validation is not a replacement for prompt engineering, but a second control layer placed after the model response.

---

## Logging and Observability

Validation also contributes to observability. When a proposal is rejected, the system logs:
- the issue key,
- the file,
- the line,
- and the reason why the validation failed.

This is useful for:
- debugging the agent,
- evaluating how often the model produces unusable suggestions,
- and understanding the real behaviour of the system in experimental runs.

The execution summary also includes the number of issues dropped after patch validation, which makes the effect of this stage visible in the logs.

---

## Limitations and Possible Future Work

The current validation strategy is a good baseline, but it could be extended in future iterations.

Possible improvements include:

- Java validation through Maven or Gradle compilation,
- TypeScript validation through `tsc --noEmit`,
- C/C++ validation through compiler metadata such as `compile_commands.json`,
- validation of required imports,
- project-level syntax checks for more languages,
- and even optional test execution for selected fixes.

These extensions would increase confidence, but they would also increase complexity and reduce portability. For that reason, they were left as future work instead of being forced into the current MVP.

---

## Conclusion

Validation is one of the most important reliability features in CodeGuardian. It ensures that generated suggestions are not published blindly and that the final feedback shown in Bitbucket is at least structurally and contextually consistent with the real repository state.

The current strategy is intentionally pragmatic: it does not try to solve everything, but it does solve the most immediate and dangerous failure cases. This makes it a strong improvement over a pure prompt-based review workflow and an important step towards a more trustworthy automated review assistant.