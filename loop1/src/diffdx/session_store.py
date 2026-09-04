"""Storage for live (in-flight) diagnostic sessions — week1.md Task 6.

Redis-backed when REDIS_URL is set, falling back to a plain in-memory dict
otherwise (single-process only; no restart survival, no sharing across
replicas — logged as a startup warning via config.log_disabled_integrations).

Completed sessions additionally persist to Postgres (DiagnosticSession +
SessionTurn, see routers/sessions.py) — this module only holds the
in-flight/live state, same split week1.md describes.
"""
from __future__ import annotations

import json
import logging

import redis
from fastapi import HTTPException

from diffdx.config import settings
from web.api_session import APISession

_log = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 2 * 60 * 60  # 2 hours, refreshed on every save

_USE_REDIS: bool = bool(settings.redis_url)

# ── Redis client (lazy singleton, same pattern as legacy_store.py's
# _get_pg()/_get_sqlite()) ──────────────────────────────────────────────
_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _redis_key(session_id: str) -> str:
    return f"diffdx:session:{session_id}"


# ── In-memory fallback (used when REDIS_URL is unset) ───────────────────
_sessions: dict[str, APISession] = {}


def get_session(session_id: str) -> APISession | None:
    if not _USE_REDIS:
        return _sessions.get(session_id)
    try:
        raw = _get_redis().get(_redis_key(session_id))
    except redis.RedisError as exc:
        _log.error("Redis unavailable reading session %s: %s", session_id, exc)
        raise HTTPException(status_code=503, detail="Session store unavailable.") from exc
    if raw is None:
        return None
    return APISession.from_dict(json.loads(raw))


def save_session(session_id: str, session: APISession, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
    if not _USE_REDIS:
        _sessions[session_id] = session
        return
    try:
        payload = json.dumps(session.to_dict())
        _get_redis().set(_redis_key(session_id), payload, ex=ttl_seconds)
    except redis.RedisError as exc:
        _log.error("Redis unavailable saving session %s: %s", session_id, exc)
        raise HTTPException(status_code=503, detail="Session store unavailable.") from exc


def delete_session(session_id: str) -> None:
    if not _USE_REDIS:
        _sessions.pop(session_id, None)
        return
    try:
        _get_redis().delete(_redis_key(session_id))
    except redis.RedisError as exc:
        _log.error("Redis unavailable deleting session %s: %s", session_id, exc)
        raise HTTPException(status_code=503, detail="Session store unavailable.") from exc
