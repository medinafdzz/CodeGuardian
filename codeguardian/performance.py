import fnmatch
import hashlib
import json
import os
import re
import time
from collections.abc import Iterable

import google.genai as genai
from google.genai import types

from codeguardian.config import CACHE_MODEL, load_batch_cache, save_batch_cache
from codeguardian.diff import changed_files, diff_base_ref, run_git
from codeguardian.logging_utils import logger
from codeguardian.models import AnalysisMetrics, Decision, Issue, IssueBatchDecision, PerformanceCandidate
from codeguardian.text import detect_language, read_file_lines, resolve_scope
from codeguardian.tokens import token_count


PERFORMANCE_REVIEW_RULES = """
You are CodeGuardian Optimization Review.

Goal:
- Suggest performance-oriented optimizations for changed functions, methods or build/configuration files.
- Focus on reducing execution time, build time, network/database overhead, repeated IO, memory pressure or algorithmic cost.
- Estimate both time complexity and space complexity when they apply.
- This review is complementary to SonarQube and must not report general SonarQube-style defects.

Strict rules:
- Only suggest a change when there is a clear expected efficiency improvement.
- Prefer substantial optimizations over small cleanups: better algorithms, fewer repeated passes, lower IO/network/database calls, reduced build work, or safer reuse of already available data.
- Analyze the candidate itself. Do not assume a fixed pattern such as nested loops or repeated membership checks.
- Consider these generic optimization families when relevant: algorithmic complexity, data structure choice, repeated computation, repeated sorting, repeated parsing/serialization, unnecessary materialization, avoidable IO inside loops, avoidable remote calls inside loops, redundant build steps, missing incremental build/cache usage, and expensive work done eagerly instead of lazily.
- Include both current and proposed estimates in original_complexity and proposed_complexity. Use a concise format such as "Time: O(n^2), Space: O(n)" or "Build: repeated dependency download, Runtime: unchanged" when Big O is not the right model.
- Do not suggest style-only refactors.
- Do not suggest micro-optimizations unless they materially reduce runtime, build time, IO, network/database calls or memory use.
- Preserve observable behaviour.
- Do not invent APIs, imports, dependencies or unavailable data structures.
- Do not change public method signatures unless absolutely necessary and safe.
- Do not introduce concurrency, caching, global state or memoization unless it is clearly safe in the local context.
- Do not trade correctness for speed.
- If no meaningful optimization can be confidently proposed from the provided context, return an empty issues list.
- If the proposed code may not compile or parse, return an empty issues list.
- Return only valid JSON.
- Use source "optimization".
- Use severity "OPTIMIZATION".
- Use sonar_key values starting with "OPTIMIZATION:".
- original_code must be copied exactly from the candidate code.
- proposed_code must be a direct replacement for original_code.
- Include original_complexity, proposed_complexity and complexity_justification.
"""

PERFORMANCE_EXCLUSIONS = (
    "tests/",
    "test/",
    "__tests__/",
    "*Test.java",
    "*Tests.java",
    "test_*.py",
    "*_test.py",
)

OPTIMIZATION_FILE_PATTERNS = {
    "Jenkinsfile": "groovy",
    "Dockerfile": "dockerfile",
    "docker-compose.yml": "yaml",
    "docker-compose.yaml": "yaml",
    "pom.xml": "xml",
    "build.gradle": "gradle",
    "build.gradle.kts": "kotlin",
    "package.json": "json",
    "requirements.txt": "requirements",
    "pyproject.toml": "toml",
    "go.mod": "go-mod",
    "Makefile": "makefile",
}

CONFIG_OPTIMIZATION_EXCLUSIONS = (
    "Jenkinsfile",
    "*.yml",
    "*.yaml",
    "*.json",
    "*.md",
    "*.xml",
    "*.properties",
    "*.ini",
    "pom.xml",
    "package-lock.json",
    "yarn.lock",
    "target/",
    "build/",
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
)

OPTIMIZATION_CACHE_KEY_VERSION = "optimization-v2"


def env_bool(primary: str, default: str = "false", fallback: str | None = None) -> bool:
    raw_value = os.getenv(primary)
    if raw_value is None and fallback:
        raw_value = os.getenv(fallback)
    return (raw_value if raw_value is not None else default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def env_int(primary: str, default: int, fallback: str | None = None) -> int:
    raw_value = os.getenv(primary)
    if raw_value is None and fallback:
        raw_value = os.getenv(fallback)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def demo_fast_mode() -> bool:
    return env_bool("CODEGUARDIAN_DEMO_FAST_MODE")


def performance_enabled() -> bool:
    return env_bool(
        "CODEGUARDIAN_ENABLE_OPTIMIZATION_REVIEW",
        fallback="CODEGUARDIAN_ENABLE_PERFORMANCE_REVIEW",
    )


def performance_max_scopes() -> int:
    if os.getenv("CODEGUARDIAN_OPTIMIZATION_MAX_SCOPES") is None:
        configured = env_int("CODEGUARDIAN_MAX_OPTIMIZATION_SCOPES", -1)
        if configured >= 0:
            return configured
    return env_int(
        "CODEGUARDIAN_OPTIMIZATION_MAX_SCOPES",
        5 if demo_fast_mode() else 10,
        fallback="CODEGUARDIAN_PERFORMANCE_MAX_SCOPES",
    )


def optimization_only_changed_files() -> bool:
    return env_bool(
        "CODEGUARDIAN_OPTIMIZATION_ONLY_CHANGED_FILES",
        "true" if demo_fast_mode() else "false",
    )


def optimization_batch_size() -> int:
    return max(1, env_int(
        "CODEGUARDIAN_OPTIMIZATION_BATCH_SIZE",
        3 if demo_fast_mode() else 1,
    ))


def skip_optimization_for_config_files() -> bool:
    return env_bool(
        "CODEGUARDIAN_SKIP_OPTIMIZATION_FOR_CONFIG_FILES",
        "true" if demo_fast_mode() else "false",
    )


def performance_min_complexity_gain() -> bool:
    return env_bool(
        "CODEGUARDIAN_OPTIMIZATION_REQUIRE_CLEAR_GAIN",
        "true",
        fallback="CODEGUARDIAN_PERFORMANCE_MIN_COMPLEXITY_GAIN",
    )


def performance_context_window() -> int:
    return env_int(
        "CODEGUARDIAN_OPTIMIZATION_CONTEXT_WINDOW",
        20,
        fallback="CODEGUARDIAN_PERFORMANCE_CONTEXT_WINDOW",
    )


def optimization_file_language(path: str) -> str:
    normalized_path = path.replace("\\", "/")
    name = os.path.basename(normalized_path)
    if name in OPTIMIZATION_FILE_PATTERNS:
        return OPTIMIZATION_FILE_PATTERNS[name]
    if normalized_path.endswith(".github/workflows/") or "/.github/workflows/" in normalized_path:
        return "yaml"
    if name.endswith((".yml", ".yaml")) and any(part in normalized_path for part in ("jenkins", "pipeline", "workflow", "ci")):
        return "yaml"
    if name.endswith((".sh", ".ps1", ".bat", ".cmd")):
        return "shell"
    return "unknown"


def performance_rules_hash() -> str:
    return hashlib.sha256(PERFORMANCE_REVIEW_RULES.encode("utf-8")).hexdigest()


def is_performance_path_excluded(path: str) -> bool:
    normalized_path = path.replace("\\", "/").lstrip("./")
    patterns = PERFORMANCE_EXCLUSIONS
    if skip_optimization_for_config_files():
        patterns = (*patterns, *CONFIG_OPTIMIZATION_EXCLUSIONS)

    for pattern in patterns:
        normalized_pattern = pattern.replace("\\", "/").lstrip("./")
        if normalized_pattern.endswith("/") and normalized_path.startswith(normalized_pattern):
            return True
        if fnmatch.fnmatch(normalized_path, normalized_pattern):
            return True
    return False


def normalized_code_hash(code: str) -> str:
    normalized = "\n".join(line.rstrip() for line in code.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def has_obvious_loop(code: str) -> bool:
    lowered = code.lower()
    return any(token in lowered for token in (
        "for ",
        "while ",
        ".map(",
        ".filter(",
        ".foreach(",
        " stream()",
        "select ",
        "join ",
    ))


def candidate_rank(candidate: PerformanceCandidate, changed_file_set: set[str]) -> tuple[int, int, int]:
    return (
        1 if candidate.file in changed_file_set else 0,
        1 if has_obvious_loop(candidate.code) else 0,
        len(candidate.code.splitlines()),
    )


def rank_and_limit_candidates(
    candidates: list[PerformanceCandidate],
    changed_file_set: set[str],
    max_scope_count: int,
) -> list[PerformanceCandidate]:
    meaningful = [
        candidate for candidate in candidates
        if candidate.target_type == "file"
        or has_obvious_loop(candidate.code)
        or len(candidate.code.splitlines()) >= 6
    ]
    ranked = sorted(
        meaningful,
        key=lambda candidate: candidate_rank(candidate, changed_file_set),
        reverse=True,
    )
    return ranked[:max_scope_count]


def changed_line_numbers(base_ref: str, file_path: str) -> list[int]:
    if not base_ref:
        return []

    diff = run_git(["diff", "--unified=0", base_ref, "--", file_path])
    lines: list[int] = []

    for match in re.finditer(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff):
        start = int(match.group(1))
        length = int(match.group(2) or "1")
        if length <= 0:
            continue
        lines.extend(range(start, start + length))

    return lines


def collect_performance_candidates(max_scopes: int | None = None) -> list[PerformanceCandidate]:
    max_scope_count = max_scopes if max_scopes is not None else performance_max_scopes()
    base_ref = diff_base_ref()
    files = changed_files(base_ref, max_files=max(max_scope_count * 8, 20))
    changed_file_set = set(files)
    logger.info("Optimization changed files detected: %s", len(files))
    if optimization_only_changed_files() and not base_ref:
        logger.info("Optimization changed-file filtering requested but no diff base was found")
        if demo_fast_mode():
            return []

    candidates: list[PerformanceCandidate] = []
    seen: set[tuple[str, str, str, int, int]] = set()
    excluded_file_count = 0
    before_file_exclusion = len(files)

    for file_path in files:
        if is_performance_path_excluded(file_path):
            excluded_file_count += 1
            continue

        language = detect_language(file_path)
        file_level_language = optimization_file_language(file_path)
        if language == "unknown":
            language = file_level_language
        if language == "unknown":
            continue

        try:
            file_lines = read_file_lines(file_path)
        except Exception:
            continue

        changed_lines = changed_line_numbers(base_ref, file_path)
        if file_level_language != "unknown" and language == file_level_language:
            key = (file_path, "file", os.path.basename(file_path), 1, len(file_lines))
            if key not in seen and file_lines:
                seen.add(key)
                candidates.append(PerformanceCandidate(
                    file=file_path,
                    target_type="file",
                    target_name=os.path.basename(file_path),
                    start_line=1,
                    end_line=len(file_lines),
                    language=language,
                    code="".join(file_lines).rstrip(),
                ))
            continue

        for line_number in changed_lines:
            scope = resolve_scope(file_path, line_number)
            if scope.kind not in {"function", "method"}:
                continue

            key = (file_path, scope.kind, scope.name, scope.start_line, scope.end_line)
            if key in seen:
                continue
            if scope.end_line > len(file_lines):
                continue

            seen.add(key)
            candidates.append(PerformanceCandidate(
                file=file_path,
                target_type=scope.kind,
                target_name=scope.name,
                start_line=scope.start_line,
                end_line=scope.end_line,
                language=language,
                code="".join(file_lines[scope.start_line - 1:scope.end_line]).rstrip(),
            ))

    selected = rank_and_limit_candidates(candidates, changed_file_set, max_scope_count)
    logger.info("Optimization candidate scopes before filtering: %s", len(candidates))
    logger.info(
        "Optimization candidates after changed-file filtering: %s",
        len(candidates) if optimization_only_changed_files() else "not enabled",
    )
    logger.info(
        "Optimization files after exclusion: %s/%s kept",
        before_file_exclusion - excluded_file_count,
        before_file_exclusion,
    )
    logger.info("Optimization selected candidate scopes: %s", len(selected))
    logger.info("Optimization max scope limit: %s", max_scope_count)

    return selected


def performance_issue_key(candidate: PerformanceCandidate) -> str:
    payload = {
        "review_type": "optimization",
        "file": candidate.file,
        "target_type": candidate.target_type,
        "target_name": candidate.target_name,
        "start_line": candidate.start_line,
        "end_line": candidate.end_line,
        "scope_content_hash": hashlib.sha256(candidate.code.encode("utf-8")).hexdigest(),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return f"OPTIMIZATION:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def performance_batch_signature(project_key: str, candidate: PerformanceCandidate) -> str:
    return performance_batch_signature_for_candidates(project_key, [candidate])


def performance_batch_signature_for_candidates(project_key: str, candidates: Iterable[PerformanceCandidate]) -> str:
    payload = {
        "review_type": "optimization",
        "cache_key_version": OPTIMIZATION_CACHE_KEY_VERSION,
        "project_key": project_key,
        "model": CACHE_MODEL,
        "rules_hash": performance_rules_hash(),
        "candidates": [
            {
                "file": candidate.file,
                "target_type": candidate.target_type,
                "target_name": candidate.target_name,
                "scope_content_hash": normalized_code_hash(candidate.code),
            }
            for candidate in candidates
        ],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_performance_prompt(project_key: str, candidate: PerformanceCandidate) -> str:
    return build_performance_batch_prompt(project_key, [candidate])


def build_performance_batch_prompt(project_key: str, candidates: list[PerformanceCandidate]) -> str:
    complexity_gain_required = performance_min_complexity_gain()
    context_window = performance_context_window()
    candidate_payloads = [
        {
            "internal_key": performance_issue_key(candidate),
            "file": candidate.file,
            "target_type": candidate.target_type,
            "target_name": candidate.target_name,
            "line": candidate.start_line,
            "original_start_line": candidate.start_line,
            "original_end_line": candidate.end_line,
            "language": candidate.language,
            "code": candidate.code,
        }
        for candidate in candidates
    ]
    return f"""
{PERFORMANCE_REVIEW_RULES}

Project:
{project_key}

Optimization review settings:
- clear efficiency gain required: {complexity_gain_required}
- context window: {context_window}

Return at most one issue per optimization candidate. If no safe optimization is clear for a candidate,
return no issue for that candidate.
The response must include current time/space or build/runtime cost estimates, proposed estimates,
optimization justification and minimal direct replacement code.
Keep every issue independent. Do not merge unrelated candidates into one code replacement.

OPTIMIZATION CANDIDATES:
{json.dumps(candidate_payloads, indent=2)}
"""


def has_required_performance_metadata(issue: Issue) -> bool:
    return bool(
        (issue.original_complexity or "").strip()
        and (issue.proposed_complexity or "").strip()
        and (issue.complexity_justification or "").strip()
    )


def analyze_performance(project_key: str) -> Decision:
    if not performance_enabled():
        logger.info("Optimization review disabled")
        return Decision(issues=[])

    logger.info("Demo fast mode enabled: %s", demo_fast_mode())
    logger.info("Optimization only changed files: %s", optimization_only_changed_files())
    logger.info("Optimization skip config files: %s", skip_optimization_for_config_files())
    candidates = collect_performance_candidates()
    logger.info("Optimization review enabled: candidate scopes=%s", len(candidates))

    if not candidates:
        return Decision(
            issues=[],
            metrics=AnalysisMetrics(performance_candidates=0),
        )

    client = genai.Client(api_key=os.getenv("LLM_AUTH_TOKEN"))
    batch_cache = load_batch_cache()
    batch_cache_hits = 0
    batch_cache_misses = 0
    batch_cache_changed = False
    issues: list[Issue] = []
    prompt_tokens = 0
    response_tokens = 0
    total_tokens = 0
    start_time = time.time()
    batch_size = optimization_batch_size()
    batches = [
        candidates[index:index + batch_size]
        for index in range(0, len(candidates), batch_size)
    ]

    logger.info("Optimization batch size: %s", batch_size)
    logger.info("Optimization review batches: %s", len(batches))
    logger.info("Optimization cache key version: %s", OPTIMIZATION_CACHE_KEY_VERSION)

    for batch in batches:
        cache_key = performance_batch_signature_for_candidates(project_key, batch)
        cached_response_text = batch_cache.get(cache_key)

        if cached_response_text:
            try:
                decision = IssueBatchDecision.model_validate_json(cached_response_text)
                batch_cache_hits += 1
            except Exception:
                decision = None
        else:
            decision = None

        if decision is None:
            batch_cache_misses += 1
            response = client.models.generate_content(
                model=CACHE_MODEL,
                contents=build_performance_batch_prompt(project_key, batch),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=IssueBatchDecision,
                    temperature=0,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )

            if response.usage_metadata:
                prompt_tokens += token_count(response.usage_metadata.prompt_token_count)
                response_tokens += token_count(response.usage_metadata.candidates_token_count)
                total_tokens += token_count(response.usage_metadata.total_token_count)

            response_text = response.text
            if response_text is None:
                logger.error(
                    "Optimization review response for batch %s did not contain text",
                    [f"{candidate.file}:{candidate.start_line}" for candidate in batch],
                )
                continue

            try:
                decision = IssueBatchDecision.model_validate_json(response_text)
            except Exception as e:
                logger.error(
                    "Failed to parse optimization review response for batch %s: %s",
                    [f"{candidate.file}:{candidate.start_line}" for candidate in batch],
                    e,
                )
                logger.error("The response from the model was: %s", response_text)
                continue

            batch_cache[cache_key] = response_text
            batch_cache_changed = True

        candidate_by_key = {performance_issue_key(candidate): candidate for candidate in batch}
        used_candidate_keys: set[str] = set()
        for issue in decision.issues[:len(batch)]:
            candidate = candidate_by_key.get(issue.sonar_key)
            if candidate is None:
                candidate = next(
                    (
                        fallback_candidate for fallback_candidate in batch
                        if performance_issue_key(fallback_candidate) not in used_candidate_keys
                    ),
                    None,
                )
            if candidate is None:
                continue
            used_candidate_keys.add(performance_issue_key(candidate))
            if not has_required_performance_metadata(issue):
                logger.info(
                    "Dropped optimization suggestion for %s:%s because cost metadata is incomplete",
                    candidate.file,
                    candidate.start_line,
                )
                continue

            issue.source = "optimization"
            issue.severity = "OPTIMIZATION"
            issue.sonar_key = performance_issue_key(candidate)
            issue.file = candidate.file
            issue.target_type = candidate.target_type
            issue.target_name = candidate.target_name
            issue.line = candidate.start_line
            issue.original_start_line = candidate.start_line
            issue.original_end_line = candidate.end_line
            issues.append(issue)

    if batch_cache_changed:
        save_batch_cache(batch_cache)

    logger.info("Optimization batch cache hits: %s", batch_cache_hits)
    logger.info("Optimization batch cache misses: %s", batch_cache_misses)
    logger.info("Optimization suggestions generated: %s", len(issues))

    return Decision(
        issues=issues,
        metrics=AnalysisMetrics(
            latency_seconds=time.time() - start_time,
            prompt_tokens=prompt_tokens,
            response_tokens=response_tokens,
            total_tokens=total_tokens,
            batch_cache_hits=batch_cache_hits,
            batch_cache_misses=batch_cache_misses,
            performance_candidates=len(candidates),
            performance_suggestions=len(issues),
        ),
    )
