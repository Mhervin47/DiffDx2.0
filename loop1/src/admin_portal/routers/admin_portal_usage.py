"""
Live-session usage panel — admin portal, Phase 2, Task 6.

Two data sources, deliberately kept distinct (see module-level NOTES below
and the mandatory coverage note in _COVERAGE_NOTE):
  - diagnostic_sessions (Postgres, durable) -> sessions_today, daily[].sessions
  - llm_usage_events (Postgres too, under Decision Gate D1 Option B — still
    durable, unlike the brief's original JSONL default) -> everything else
    (tokens, latency, cost)

Phase 1's two mandatory cost notes ("critic never runs live", "profile
updater runs every turn") are WRONG for this live path — see build brief
Section 1.2. This module's own coverage note corrects that; do not copy
Phase 1's notes here. Required verbatim in three places (build brief
Section 11.2): this docstring, every /api/admin/usage response body
(the "coverage_note" field), and visibly on the usage panel
(usage.js renders it as permanent text, not a tooltip):

    "Usage figures cover the doctor model's own call only. In the live web
    session the profile updater is disabled by design, but the compressor
    runs once history exceeds the keep-recent window, and the critic runs
    on every turn in a background thread — neither reports token usage,
    because both go through call_llm(), which discards it. True
    per-session cost is therefore higher than shown. Completion tokens are
    estimated from output length, not measured: llm.py returns prompt
    tokens only. llm.py also falls back to a different provider on HTTP
    429 without reporting which model actually served the request, so
    per-model cost attribution is approximate."
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from diffdx.db.engine import get_sessionmaker
from diffdx.db.models.clinical import DiagnosticSession
from diffdx.db.models.usage import LlmUsageEvent
from diffdx.dependencies import require_role

router = APIRouter(tags=["admin_portal"])
_log = logging.getLogger(__name__)

# routers/admin_portal_usage.py -> routers -> admin_portal -> src -> loop1 (4 parent hops)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_PRICING_PATH = _REPO_ROOT / "src" / "admin_portal" / "eval" / "pricing.json"

_DEFAULT_WINDOW_DAYS = 14
_MAX_WINDOW_DAYS = 90
_DEFAULT_SOURCE = "live_web"
_MIN_SAMPLE_FOR_P95 = 20

# Required verbatim (or near it) in this module's docstring, in every
# response, and visibly on the panel — see build brief Section 11.2. Phase
# 1's two cost notes describe the CLI Session path (phase7_eval.py) and are
# wrong about this one; do not conflate them.
_COVERAGE_NOTE = (
    "Usage figures cover the doctor model's own call only. In the live web "
    "session the profile updater is disabled by design, but the compressor "
    "runs once history exceeds the keep-recent window, and the critic runs "
    "on every turn in a background thread — neither reports token usage, "
    "because both go through call_llm(), which discards it. True "
    "per-session cost is therefore higher than shown. Completion tokens are "
    "estimated from output length, not measured: llm.py returns prompt "
    "tokens only. llm.py also falls back to a different provider on HTTP "
    "429 without reporting which model actually served the request, so "
    "per-model cost attribution is approximate."
)

_EPHEMERALITY_NOTE = (
    "Under Decision Gate D1 Option B, usage records are stored in Postgres "
    "(the llm_usage_events table), not the ephemeral container filesystem — "
    "so unlike the brief's default JSONL design, token/cost/latency history "
    "here DOES survive restarts and deploys, same as session counts."
)


def _load_pricing() -> dict[str, Any]:
    try:
        with open(_PRICING_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        _log.warning("Could not load pricing.json at %s", _PRICING_PATH, exc_info=True)
        return {"_default": {"input_usd_per_1k": 0.0005, "output_usd_per_1k": 0.0015}}


def _rate_for_model(pricing: dict, model: str | None) -> dict:
    if model and model in pricing:
        return pricing[model]
    return pricing["_default"]


def _estimate_cost(prompt_tokens: float | None, completion_tokens: float | None, rates: dict) -> float | None:
    if prompt_tokens is None or completion_tokens is None:
        return None
    return (prompt_tokens / 1000 * rates["input_usd_per_1k"]) + (completion_tokens / 1000 * rates["output_usd_per_1k"])


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


@router.get("/api/admin/usage")
def get_usage(
    days: int = Query(default=_DEFAULT_WINDOW_DAYS, ge=1, le=_MAX_WINDOW_DAYS),
    _admin: dict = Depends(require_role("admin")),
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    today = now.date()

    with get_sessionmaker()() as db:
        # --- Durable source: diagnostic_sessions (Postgres) ---
        sessions_in_window = db.execute(
            select(func.count()).select_from(DiagnosticSession)
            .where(DiagnosticSession.started_at >= window_start)
        ).scalar_one()

        sessions_today = db.execute(
            select(func.count()).select_from(DiagnosticSession)
            .where(func.date(DiagnosticSession.started_at) == today)
        ).scalar_one()

        session_rows = db.execute(
            select(DiagnosticSession.started_at)
            .where(DiagnosticSession.started_at >= window_start)
        ).all()
        sessions_by_day: dict[date, int] = {}
        for (started_at,) in session_rows:
            if started_at is None:
                continue
            d = started_at.date() if hasattr(started_at, "date") else started_at
            sessions_by_day[d] = sessions_by_day.get(d, 0) + 1

        # --- Non-Postgres-fallback-but-still-durable-here source (D1-B):
        # llm_usage_events ---
        all_in_window = db.execute(
            select(LlmUsageEvent).where(LlmUsageEvent.recorded_at >= window_start)
        ).scalars().all()

    usage_records_total = len(all_in_window)
    live_events = [e for e in all_in_window if e.source == _DEFAULT_SOURCE]
    excluded_by_source = usage_records_total - len(live_events)

    if not live_events:
        return {
            "status": "no_data",
            "message": (
                "No usage data yet for source='live_web' in this window — usage accumulates "
                "only after a real session runs through the instrumented api_session.py path."
            ),
            "window_days": days,
            "coverage_note": _COVERAGE_NOTE,
            "notes": [_EPHEMERALITY_NOTE],
        }

    session_ids_with_usage = {e.session_id for e in live_events}
    n_usage_sessions = len(session_ids_with_usage)

    sum_prompt = sum((e.prompt_tokens or 0) for e in live_events)
    sum_completion = sum((e.completion_tokens_estimated or 0) for e in live_events)
    mean_prompt_per_session = sum_prompt / n_usage_sessions
    mean_completion_per_session = sum_completion / n_usage_sessions
    mean_tokens_per_session = mean_prompt_per_session + mean_completion_per_session

    latencies = [e.latency_ms for e in live_events if e.latency_ms is not None]
    p95_latency = _percentile(latencies, 0.95)
    p95_note = None
    if latencies and len(latencies) < _MIN_SAMPLE_FOR_P95:
        p95_note = f"Sample too small for a stable p95 (n={len(latencies)}, want >= {_MIN_SAMPLE_FOR_P95})."

    pricing = _load_pricing()
    # config["models"]["doctor"] is the configured model; llm.py's fallback
    # chain may have actually served a different one on a 429 (1.8) — this
    # is the same approximation _COVERAGE_NOTE already discloses.
    from loop1.config import config as loop1_config
    model_configured = loop1_config.get("models", {}).get("doctor")
    rates = _rate_for_model(pricing, model_configured)
    cost_per_session = _estimate_cost(mean_prompt_per_session, mean_completion_per_session, rates)

    daily: list[dict[str, Any]] = []
    events_by_day: dict[date, list[LlmUsageEvent]] = {}
    for e in live_events:
        d = e.recorded_at.date() if hasattr(e.recorded_at, "date") else e.recorded_at
        events_by_day.setdefault(d, []).append(e)

    for i in range(days):
        d = (now - timedelta(days=days - 1 - i)).date()
        day_events = events_by_day.get(d, [])
        day_prompt = sum((e.prompt_tokens or 0) for e in day_events)
        day_completion = sum((e.completion_tokens_estimated or 0) for e in day_events)
        day_cost = _estimate_cost(day_prompt, day_completion, rates) if day_events else 0.0
        daily.append({
            "date": d.isoformat(),
            "sessions": sessions_by_day.get(d, 0),
            "turns": len(day_events),
            "cost_usd_estimated": round(day_cost, 6) if day_cost is not None else 0.0,
        })

    return {
        "status": "ok",
        "window_days": days,
        "cost_per_session_usd_estimated": round(cost_per_session, 6) if cost_per_session is not None else None,
        "mean_tokens_per_session": round(mean_tokens_per_session, 1),
        "p95_llm_latency_ms": round(p95_latency, 1) if p95_latency is not None else None,
        "sessions_today": sessions_today,
        "daily": daily,
        "counts": {
            "usage_records": usage_records_total,
            "sessions_in_window": sessions_in_window,
            "records_excluded_by_source_filter": excluded_by_source,
            # Always 0 under Decision Gate D1 Option B (Postgres) — the
            # brief's "malformed_lines_skipped" count is a JSONL-parsing
            # concept (Option A); a DB row either matches the schema or
            # doesn't exist. Field kept for contract compatibility.
            "malformed_lines_skipped": 0,
        },
        "coverage_note": _COVERAGE_NOTE,
        "notes": [_EPHEMERALITY_NOTE] + ([p95_note] if p95_note else []),
    }
