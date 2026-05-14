import fnmatch
import hashlib
import json
import os
import re
import time

import google.genai as genai
from google.genai import types

from codeguardian.config import CACHE_MODEL, load_batch_cache, save_batch_cache
from codeguardian.diff import changed_files, diff_base_ref, run_git
from codeguardian.logging_utils import logger
from codeguardian.models import AnalysisMetrics, Decision, Issue, IssueBatchDecision, PerformanceCandidate
from codeguardian.text import detect_language, read_file_lines, resolve_scope
from codeguardian.tokens import token_count


PERFORMANCE_REVIEW_RULES = """
You are CodeGuardian Performance Review.

Goal:
- Suggest performance-oriented improvements for changed functions or methods only.
- Focus on inefficient logic where algorithmic complexity can be improved with Big O reasoning.
- This review is complementary to SonarQube and must not report general SonarQube-style defects.

Strict rules:
- Only suggest a change when there is a clear expected efficiency improvement.
- Prefer asymptotic improvements such as O(n^2) to O(n), O(n log n) to O(n), or repeated linear search to hash-based lookup.
- Do not suggest style-only refactors.
- Do not suggest micro-optimizations unless they are clearly justified.
- Preserve observable behaviour.
- Do not invent APIs, imports, dependencies or unavailable data structures.
- Do not change public method signatures unless absolutely necessary and safe.
- Do not introduce concurrency, caching, global state or memoization unless it is clearly safe in the local context.
- Do not trade correctness for speed.
- If complexity cannot be confidently improved from the provided context, return an empty issues list.
- If the proposed code may not compile or parse, return an empty issues list.
- Return only valid JSON.
- Use source "performance".
- Use severity "PERFORMANCE".
- Use sonar_key values starting with "PERFORMANCE:".
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


def performance_enabled() -> bool:
    return os.getenv("CODEGUARDIAN_ENABLE_PERFORMANCE_REVIEW", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def performance_max_scopes() -> int:
    return int(os.getenv("CODEGUARDIAN_PERFORMANCE_MAX_SCOPES", "10"))


def performance_min_complexity_gain() -> bool:
    return os.getenv("CODEGUARDIAN_PERFORMANCE_MIN_COMPLEXITY_GAIN", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def performance_context_window() -> int:
    return int(os.getenv("CODEGUARDIAN_PERFORMANCE_CONTEXT_WINDOW", "20"))


def performance_rules_hash() -> str:
    return hashlib.sha256(PERFORMANCE_REVIEW_RULES.encode("utf-8")).hexdigest()


def is_performance_path_excluded(path: str) -> bool:
    normalized_path = path.replace("\\", "/").lstrip("./")
    for pattern in PERFORMANCE_EXCLUSIONS:
        normalized_pattern = pattern.replace("\\", "/").lstrip("./")
        if normalized_pattern.endswith("/") and normalized_path.startswith(normalized_pattern):
            return True
        if fnmatch.fnmatch(normalized_path, normalized_pattern):
            return True
    return False


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
    files = changed_files(base_ref, max_files=max_scope_count * 2)
    candidates: list[PerformanceCandidate] = []
    seen: set[tuple[str, str, str, int, int]] = set()

    for file_path in files:
        if is_performance_path_excluded(file_path):
            continue

        language = detect_language(file_path)
        if language == "unknown":
            continue

        try:
            file_lines = read_file_lines(file_path)
        except Exception:
            continue

        for line_number in changed_line_numbers(base_ref, file_path):
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

            if len(candidates) >= max_scope_count:
                return candidates

    return candidates


def performance_issue_key(candidate: PerformanceCandidate) -> str:
    payload = {
        "review_type": "performance",
        "file": candidate.file,
        "target_type": candidate.target_type,
        "target_name": candidate.target_name,
        "start_line": candidate.start_line,
        "end_line": candidate.end_line,
        "scope_content_hash": hashlib.sha256(candidate.code.encode("utf-8")).hexdigest(),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return f"PERFORMANCE:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def performance_batch_signature(project_key: str, candidate: PerformanceCandidate) -> str:
    payload = {
        "review_type": "performance",
        "project_key": project_key,
        "model": CACHE_MODEL,
        "rules_hash": performance_rules_hash(),
        "file": candidate.file,
        "target_type": candidate.target_type,
        "target_name": candidate.target_name,
        "start_line": candidate.start_line,
        "end_line": candidate.end_line,
        "scope_content_hash": hashlib.sha256(candidate.code.encode("utf-8")).hexdigest(),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_performance_prompt(project_key: str, candidate: PerformanceCandidate) -> str:
    complexity_gain_required = performance_min_complexity_gain()
    context_window = performance_context_window()
    return f"""
{PERFORMANCE_REVIEW_RULES}

Project:
{project_key}

Performance review settings:
- minimum asymptotic complexity gain required: {complexity_gain_required}
- context window: {context_window}

Return one issue at most. If no safe performance improvement is clear, return an empty issues list.
The response must include the current complexity estimate, proposed complexity estimate,
complexity justification and minimal direct replacement code.

PERFORMANCE CANDIDATE:
{json.dumps({
    "internal_key": performance_issue_key(candidate),
    "file": candidate.file,
    "target_type": candidate.target_type,
    "target_name": candidate.target_name,
    "line": candidate.start_line,
    "original_start_line": candidate.start_line,
    "original_end_line": candidate.end_line,
    "language": candidate.language,
    "code": candidate.code,
}, indent=2)}
"""


def has_required_performance_metadata(issue: Issue) -> bool:
    return bool(
        (issue.original_complexity or "").strip()
        and (issue.proposed_complexity or "").strip()
        and (issue.complexity_justification or "").strip()
    )


def analyze_performance(project_key: str) -> Decision:
    if not performance_enabled():
        return Decision(issues=[])

    candidates = collect_performance_candidates()
    logger.info("Performance review enabled: candidate scopes=%s", len(candidates))

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

    logger.info("Performance review batches: %s", len(candidates))

    for candidate in candidates:
        cache_key = performance_batch_signature(project_key, candidate)
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
                contents=build_performance_prompt(project_key, candidate),
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
                    "Performance review response for %s:%s did not contain text",
                    candidate.file,
                    candidate.start_line,
                )
                continue

            try:
                decision = IssueBatchDecision.model_validate_json(response_text)
            except Exception as e:
                logger.error("Failed to parse performance review response for %s:%s: %s",
                             candidate.file, candidate.start_line, e)
                logger.error("The response from the model was: %s", response_text)
                continue

            batch_cache[cache_key] = response_text
            batch_cache_changed = True

        for issue in decision.issues[:1]:
            if not has_required_performance_metadata(issue):
                logger.info(
                    "Dropped performance suggestion for %s:%s because complexity metadata is incomplete",
                    candidate.file,
                    candidate.start_line,
                )
                continue

            issue.source = "performance"
            issue.severity = "PERFORMANCE"
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

    logger.info("Performance batch cache hits: %s", batch_cache_hits)
    logger.info("Performance batch cache misses: %s", batch_cache_misses)
    logger.info("Performance suggestions generated: %s", len(issues))

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
