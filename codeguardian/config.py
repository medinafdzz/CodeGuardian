import base64
import json
import os
import time
from pathlib import Path
from datetime import datetime, timezone
import hashlib

import httpx

from codeguardian.logging_utils import logger
from codeguardian.models import AgentExecutionError
from codeguardian.review_rules import REVIEW_RULES


CODEGUARDIAN_SUMMARY_TITLE = "**CodeGuardian Analysis Summary**"
CODEGUARDIAN_AGENT_MARKER = "<!-- CodeGuardian-Agent -->"
ATLASSIAN_ROVO_MCP_URL = "https://mcp.atlassian.com/v1/mcp"
CACHE_METADATA_PATH = os.getenv(
    "CACHE_METADATA_PATH",
    "/var/jenkins_home/codeguardian/gemini_prompt_cache.json",
).strip()
CACHE_MODEL = "gemini-2.5-flash"
CACHE_MODE = os.getenv("CACHE_MODE", "explicit").strip().lower()
if CACHE_MODE not in {"implicit", "explicit"}:
    CACHE_MODE = "explicit"
CACHE_TTL = os.getenv("CACHE_TTL", "3600s").strip()
BATCH_CACHE_PATH = os.getenv(
    "BATCH_CACHE_PATH",
    "/var/jenkins_home/codeguardian/gemini_batch_cache.json",
).strip()
BATCH_CACHE_MAX_AGE_SECONDS = int(os.getenv("BATCH_CACHE_MAX_AGE_SECONDS", "86400"))


def rules_hash() -> str:
    return hashlib.sha256(REVIEW_RULES.encode("utf-8")).hexdigest()


def load_cache_metadata() -> dict | None:
    path = Path(CACHE_METADATA_PATH)

    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_cache_metadata(data: dict) -> None:
    path = Path(CACHE_METADATA_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def cache_meta_valid(metadata: dict | None) -> bool:
    if not metadata:
        return False

    if metadata.get("model") != CACHE_MODEL:
        return False

    if metadata.get("ttl") != CACHE_TTL:
        return False

    if metadata.get("rules_hash") != rules_hash():
        return False

    expire_time = metadata.get("expire_time")
    if not expire_time:
        return False

    try:
        expires_at = datetime.fromisoformat(expire_time.replace("Z", "+00:00"))
    except Exception:
        return False

    return expires_at > datetime.now(timezone.utc)


def load_batch_cache() -> dict:
    path = Path(BATCH_CACHE_PATH)

    if not path.exists():
        return {}

    try:
        file_age_seconds = time.time() - path.stat().st_mtime
        if file_age_seconds > BATCH_CACHE_MAX_AGE_SECONDS:
            path.unlink(missing_ok=True)
            logger.info("Deleted expired Gemini batch cache")
            return {}
    except Exception:
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_batch_cache(data: dict) -> None:
    path = Path(BATCH_CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_atlassian_mcp_url() -> str:
    return (os.getenv("ATLASSIAN_MCP_URL") or ATLASSIAN_ROVO_MCP_URL).strip()


def get_atlassian_mcp_auth() -> httpx.Auth:
    auth_header = (os.getenv("ATLASSIAN_MCP_AUTH_HEADER") or "").strip()

    if not auth_header:
        raise AgentExecutionError("Missing ATLASSIAN_MCP_AUTH_HEADER for Atlassian Rovo MCP")

    if auth_header.startswith("Basic "):
        token = auth_header[len("Basic "):].strip()
        try:
            decoded = base64.b64decode(token).decode("utf-8")
            username, password = decoded.split(":", 1)
        except Exception as e:
            raise AgentExecutionError("Invalid Basic auth format in ATLASSIAN_MCP_AUTH_HEADER") from e

        return httpx.BasicAuth(username, password)

    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):].strip()

        class BearerAuth(httpx.Auth):

            def auth_flow(self, request):
                request.headers["Authorization"] = f"Bearer {token}"
                yield request

        return BearerAuth()

    raise AgentExecutionError("Unsupported ATLASSIAN_MCP_AUTH_HEADER scheme")
