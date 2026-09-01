import hashlib
import json
import os
import time

import google.genai as genai
from google.genai import types

from codeguardian.config import (
    CACHE_MODE,
    CACHE_MODEL,
    CACHE_TTL,
    REVIEW_RULES,
    cache_meta_valid,
    load_batch_cache,
    load_cache_metadata,
    rules_hash,
    save_batch_cache,
    save_cache_metadata,
)
from codeguardian.logging_utils import logger
from codeguardian.models import AnalysisMetrics, Decision, Issue, IssueBatchDecision
from codeguardian.text import read_file_lines
from codeguardian.tokens import token_count


def ensure_prompt_cache(client: genai.Client) -> str:
    metadata = load_cache_metadata()

    if cache_meta_valid(metadata):
        cache_name = metadata["name"]

        try:
            cache = client.caches.get(name=cache_name)
            return cache.name
        except Exception:
            pass

    cache = client.caches.create(
        model=CACHE_MODEL,
        config=types.CreateCachedContentConfig(
            system_instruction=REVIEW_RULES,
            display_name="codeguardian-review-rules",
            ttl=CACHE_TTL,
        ),
    )

    expire_time = getattr(cache, "expire_time", None)
    if expire_time is not None:
        expire_time = str(expire_time)

    save_cache_metadata({
        "name": cache.name,
        "model": CACHE_MODEL,
        "ttl": CACHE_TTL,
        "rules_hash": rules_hash(),
        "expire_time": expire_time,
    })

    return cache.name


def build_scope_batches(issues: list[dict]) -> list[list[dict]]:
    grouped: dict[tuple, list[dict]] = {}
    ordered_keys: list[tuple] = []

    for issue in sorted(
            issues,
            key=lambda item: (
                item.get("file", ""),
                int(item.get("scope_start_line", item.get("line", 0)) or 0),
                int(item.get("line", 0) or 0),
            ),
    ):
        scope_kind = issue.get("scope_kind", "global")
        scope_name = issue.get("scope_name", "")
        scope_start = int(issue.get("scope_start_line", issue.get("line", 0)) or issue.get("line", 0))
        scope_end = int(issue.get("scope_end_line", issue.get("line", 0)) or issue.get("line", 0))

        if scope_kind in {"function", "method"}:
            key = (issue.get("file", ""), scope_kind, scope_name, scope_start, scope_end)
        else:
            # Los problemas fuera de función van solos
            key = (
                issue.get("file", ""),
                "global",
                f"global:{issue.get('sonar_key', 'NO_KEY')}:{issue.get('line', 0)}",
                int(issue.get("line", 0) or 0),
                int(issue.get("line", 0) or 0),
            )

        if key not in grouped:
            grouped[key] = []
            ordered_keys.append(key)

        grouped[key].append(issue)

    return [grouped[key] for key in ordered_keys]


def batch_signature(project_key: str, batch: list[dict]) -> str:
    normalized_batch = []

    for issue in batch:
        scope_start_line = int(issue.get("scope_start_line", issue.get("line", 0)) or 0)
        scope_end_line = int(issue.get("scope_end_line", issue.get("line", 0)) or 0)
        file_path = issue.get("file", "")

        scope_content_hash = ""
        if file_path and os.path.exists(file_path) and scope_start_line > 0 and scope_end_line >= scope_start_line:
            try:
                lines = read_file_lines(file_path)
                if scope_end_line <= len(lines):
                    scope_content = "".join(lines[scope_start_line - 1:scope_end_line])
                    scope_content_hash = hashlib.sha256(scope_content.encode("utf-8")).hexdigest()
            except Exception:
                scope_content_hash = ""

        normalized_batch.append({
            "sonar_key": issue.get("sonar_key", "NO_KEY"),
            "file": file_path,
            "line": int(issue.get("line", 0) or 0),
            "severity": issue.get("severity", ""),
            "message": issue.get("message", ""),
            "code_context": issue.get("code_context", ""),
            "scope_kind": issue.get("scope_kind", "global"),
            "scope_name": issue.get("scope_name", ""),
            "scope_start_line": scope_start_line,
            "scope_end_line": scope_end_line,
            "scope_content_hash": scope_content_hash,
        })

    payload = {
        "project_key": project_key,
        "model": CACHE_MODEL,
        "rules_hash": rules_hash(),
        "batch": normalized_batch,
    }

    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def analyze_code_with_gemini(project_key: str, issues: list[dict]) -> Decision:
    client = genai.Client(api_key=os.getenv("LLM_AUTH_TOKEN"))

    logger.info("Gemini prompt cache mode: %s", CACHE_MODE)

    model_issues: list[Issue] = []
    total_prompt_tokens = 0
    total_response_tokens = 0
    total_tokens = 0
    total_cached_tokens = 0
    start_time = time.time()
    batch_cache = load_batch_cache()
    batch_cache_hits = 0
    batch_cache_misses = 0
    batch_cache_changed = False

    batches = build_scope_batches(issues)

    cached_name = None
    if CACHE_MODE == "explicit":
        cached_name = ensure_prompt_cache(client)

    for batch in batches:

        batch_scope_kind = batch[0].get("scope_kind", "global")
        batch_scope_name = batch[0].get("scope_name", "")
        batch_scope_start = int(batch[0].get("scope_start_line", batch[0].get("line", 0)) or batch[0].get("line", 0))
        batch_scope_end = int(batch[0].get("scope_end_line", batch[0].get("line", 0)) or batch[0].get("line", 0))

        if batch_scope_kind in {"function", "method"}:
            scope_instruction = f"""
            All findings in this batch belong to the same {batch_scope_kind}: '{batch_scope_name}'.
            This scope starts at line {batch_scope_start} and ends at line {batch_scope_end}.

            If a real code change is needed, return exactly one issue object for this whole scope.
            Consolidate all findings in the batch into one single refactor proposal when applicable.
            Use one original_code block and one proposed_code block covering the full scope when needed.
            Do not return multiple issue objects for the same function or method.
            If no real fix is needed, return an empty issues list.
            """
        else:
            scope_instruction = """
            This finding is outside any function or method.
            Treat it as a global or top-level issue.
            Return one issue object for this finding only.
            Do not merge it with any other scope.
            """

        if CACHE_MODE == "explicit":
            prompt = f"""
                Project:
                {project_key}

                Scope instructions:
                {scope_instruction}

                SONARQUBE DATA:
                {json.dumps(batch)}
            """
        else:
            prompt = f"""
                {REVIEW_RULES}

                Project:
                {project_key}

                Scope instructions:
                {scope_instruction}

                SONARQUBE DATA:
                {json.dumps(batch)}
            """

        cache_key = batch_signature(project_key, batch)
        cached_response_text = batch_cache.get(cache_key)

        if cached_response_text:
            try:
                partial_decision = IssueBatchDecision.model_validate_json(cached_response_text)
                batch_cache_hits += 1
            except Exception:
                partial_decision = None
        else:
            partial_decision = None

        if partial_decision is None:
            batch_cache_misses += 1

            generate_config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=IssueBatchDecision,
                temperature=0,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )

            if CACHE_MODE == "explicit":
                generate_config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=IssueBatchDecision,
                    temperature=0,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    cached_content=cached_name,
                )

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=generate_config,
            )

            if response.usage_metadata:
                total_prompt_tokens += token_count(response.usage_metadata.prompt_token_count)
                total_response_tokens += token_count(response.usage_metadata.candidates_token_count)
                total_tokens += token_count(response.usage_metadata.total_token_count)
                total_cached_tokens += token_count(response.usage_metadata.cached_content_token_count)

            response_text = response.text

            try:
                partial_decision = IssueBatchDecision.model_validate_json(response_text)
            except Exception as e:
                logger.error(
                    "Failed to parse Gemini batch response for sonar keys %s: %s",
                    [issue.get("sonar_key", "NO_KEY") for issue in batch],
                    e,
                )
                logger.error("The response from the model was: %s", response_text)
                continue

            batch_cache[cache_key] = response_text
            batch_cache_changed = True

        expected_sonar_keys = {
            issue.get("sonar_key", "NO_KEY") for issue in batch if issue.get("sonar_key", "NO_KEY") != "NO_KEY"
        }

        kept_batch_issues: dict[str, Issue] = {}

        for issue in partial_decision.issues:
            if not issue.sonar_key or issue.sonar_key == "NO_KEY":
                continue
            if issue.sonar_key not in expected_sonar_keys:
                continue
            if issue.sonar_key in kept_batch_issues:
                continue
            kept_batch_issues[issue.sonar_key] = issue

        model_issues.extend(kept_batch_issues.values())

    duration = time.time() - start_time
    logger.info("Gemini produced %s issues", len(model_issues))

    if total_cached_tokens:
        logger.info("Gemini total cached tokens: %s", total_cached_tokens)

    if batch_cache_changed:
        save_batch_cache(batch_cache)

    logger.info("Gemini batch cache hits: %s", batch_cache_hits)
    logger.info("Gemini batch cache misses: %s", batch_cache_misses)

    return Decision(
        issues=model_issues,
        metrics=AnalysisMetrics(
            latency_seconds=duration,
            prompt_tokens=total_prompt_tokens,
            response_tokens=total_response_tokens,
            total_tokens=total_tokens,
            cached_tokens=total_cached_tokens,
            batch_cache_hits=batch_cache_hits,
            batch_cache_misses=batch_cache_misses,
        ),
    )
