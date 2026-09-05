"""GET /health, /ready — week1.md Task 7 (Docker HEALTHCHECK target) and
Task 4's target layout (routers/health.py). Didn't exist anywhere before
this; needed for the Dockerfile's HEALTHCHECK to have something to hit.

/health is liveness: is the process up and answering requests at all.
/ready is readiness: can this instance actually serve traffic right now —
a real DB round trip, and a real Redis PING when REDIS_URL is configured.
Failing either returns 503, not a 500 stack trace, matching the same
"infra failure becomes 503" pattern already used in session_store.py.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from diffdx.config import settings
from diffdx.db.engine import get_sessionmaker
from diffdx.session_store import _get_redis

router = APIRouter(tags=["health"])
_log = logging.getLogger(__name__)


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready():
    try:
        with get_sessionmaker()() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:
        _log.error("Readiness check failed: database unreachable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable.") from exc

    if settings.redis_url:
        try:
            _get_redis().ping()
        except Exception as exc:
            _log.error("Readiness check failed: Redis unreachable: %s", exc)
            raise HTTPException(status_code=503, detail="Session store unavailable.") from exc

    return {"status": "ready"}
