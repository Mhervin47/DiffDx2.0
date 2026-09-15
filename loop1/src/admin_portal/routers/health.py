"""
Admin-only detailed health panel — admin portal, Phase 2, Task 5.

Distinct from src/diffdx/routers/health.py's /health and /ready (the
Dockerfile HEALTHCHECK target and the readiness probe) — those are NOT
touched here. This is a richer, per-dependency status view for the admin
UI, behind require_role("admin"), never used by infrastructure.

Every dependency check runs with its own 2-second timeout, in its own
try/except, so one dead dependency degrades one pill — it never 500s or
hangs the endpoint. Status vocabulary: "ok", "down", "not_configured".
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends
from sqlalchemy import text

from diffdx.config import settings
from diffdx.db.engine import get_sessionmaker
from diffdx.dependencies import require_role
from diffdx.session_store import _get_redis

router = APIRouter(tags=["admin_portal"])
_log = logging.getLogger(__name__)

_CHECK_TIMEOUT_SECONDS = 2.0


def _run_with_timeout(fn: Callable[[], float], timeout_s: float = _CHECK_TIMEOUT_SECONDS) -> float:
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeoutError as exc:
            raise TimeoutError(f"exceeded {timeout_s}s") from exc


def _check_postgres() -> dict[str, Any]:
    def _do() -> float:
        t0 = time.perf_counter()
        with get_sessionmaker()() as db:
            db.execute(text("SELECT 1"))
        return (time.perf_counter() - t0) * 1000

    try:
        latency_ms = _run_with_timeout(_do)
        return {"status": "ok", "latency_ms": round(latency_ms, 1)}
    except Exception as exc:
        _log.warning("Health check: postgres failed: %s", exc)
        return {"status": "down", "latency_ms": None, "detail": "Database unreachable."}


def _check_redis() -> dict[str, Any]:
    if not settings.redis_url:
        return {
            "status": "not_configured",
            "latency_ms": None,
            "detail": "REDIS_URL unset — sessions held in-memory.",
        }

    def _do() -> float:
        t0 = time.perf_counter()
        _get_redis().ping()
        return (time.perf_counter() - t0) * 1000

    try:
        latency_ms = _run_with_timeout(_do)
        return {"status": "ok", "latency_ms": round(latency_ms, 1)}
    except Exception as exc:
        _log.warning("Health check: redis failed: %s", exc)
        return {"status": "down", "latency_ms": None, "detail": "Redis unreachable."}


def _check_openrouter() -> dict[str, Any]:
    # Presence only — never a real API call, same reasoning as before: a
    # health check polled every 30s must not make a billable model call.
    # Replaces the old Groq pill: GROQ_API_KEY is REQUIRED at startup
    # (diffdx.config.Settings fails fast without it), so that pill could
    # only ever read "ok" — it never carried real information. OPENROUTER_API_KEY
    # is optional and actually gates something: without it the critic is
    # disabled entirely and loop1.llm's HTTP-429 fallback chain (_FALLBACK_CHAIN)
    # loses every openrouter/* entry — so "not configured" here is a real,
    # actionable signal, not a permanently-green pill.
    if settings.openrouter_api_key:
        return {
            "status": "ok",
            "latency_ms": None,
            "detail": "API key present. Connectivity not tested — health checks do not make billable model calls.",
        }
    return {
        "status": "not_configured",
        "latency_ms": None,
        "detail": "OPENROUTER_API_KEY unset — critic scoring disabled, and the HTTP-429 fallback chain has fewer options.",
    }


@router.get("/api/admin/health/detail")
def health_detail(_admin: dict = Depends(require_role("admin"))) -> dict[str, Any]:
    # "status": "ok" here means "this endpoint answered", matching the
    # convention every other admin_portal endpoint uses for the frontend's
    # shared fetchWithMockFallback() helper — it does not mean every
    # dependency is healthy; check the per-dependency status fields for that.
    return {
        "status": "ok",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "api": "ok",
        "postgres": _check_postgres(),
        "redis": _check_redis(),
        "openrouter": _check_openrouter(),
    }
