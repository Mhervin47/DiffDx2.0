"""
Live-session usage panel — admin portal, Phase 2, Task 6.

Two data sources, deliberately kept distinct (see module-level NOTES below
and the mandatory coverage note in _COVERAGE_NOTE):
  - diagnostic_sessions (Postgres, durable) -> sessions_today, daily[].sessions
  - llm_usage_events (Postgres too, under Decision Gate D1 Option B — still
    durable, unlike the brief's original JSONL default) -> everything else
    (tokens, latency, cost)

Phase 3 closed the two biggest gaps the original coverage note disclosed:
completion tokens/total tokens/the model that actually served each call
(post-fallback) are now real, measured values from the provider's own
response (loop1.llm.LlmUsage), not an estimate — and the compressor and
critic calls, which previously left zero trace, are now logged too
(call_site="compressor"/"critic"), so per-session cost reflects the whole
live turn, not just the doctor's own call. A later pass closed a third gap:
the closing-turn call (loop1.closing_turn.generate_closing_turn_with_usage,
web/api_session.py's _finalize()) — the one other real LLM call per session,
generating the patient-facing summary — is now logged too
(call_site="closing_turn"). The one gap still open: the
profile updater remains disabled in the live web session by design (a
CLI/offline-only code path) — see build brief Section 1.2 for why. Required
verbatim in three places (build brief Section 11.2, honored across the
Phase 3 rewrite): this docstring, every /api/admin/usage response body
(the "coverage_note" field), and visibly on the usage panel (usage.js
renders it as permanent text, not a tooltip) — see _COVERAGE_NOTE below.
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
from diffdx.db.models.usage import LlmUsageEvent, SarvamUsageEvent
from diffdx.dependencies import require_role

router = APIRouter(tags=["admin_portal"])
_log = logging.getLogger(__name__)

# routers/admin_portal_usage.py -> routers -> admin_portal -> src -> loop1 (4 parent hops)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_PRICING_PATH = _REPO_ROOT / "src" / "admin_portal" / "eval" / "pricing.json"
_SARVAM_PRICING_PATH = _REPO_ROOT / "src" / "admin_portal" / "eval" / "sarvam_pricing.json"

_DEFAULT_WINDOW_DAYS = 14
_MAX_WINDOW_DAYS = 90
_DEFAULT_SOURCE = "live_web"
_MIN_SAMPLE_FOR_P95 = 20

# Required verbatim (or near it) in this module's docstring, in every
# response, and visibly on the panel — see build brief Section 11.2. Phase
# 1's two cost notes describe the CLI Session path (phase7_eval.py) and are
# wrong about this one; do not conflate them.
_COVERAGE_NOTE = (
    "Usage figures cover the doctor model's own call, the compressor "
    "(runs once history exceeds the keep-recent window), the critic "
    "(runs on every turn in a background thread), and the closing-turn "
    "call (the patient-facing summary generated once at session end) — "
    "all four are logged separately (see call_site on each record) and "
    "summed into these totals. The profile updater is disabled by design "
    "in the live web "
    "session (a CLI/offline-only path), so its cost is not, and cannot be, "
    "represented here. Completion tokens and the exact model that served "
    "each call (which can differ from the configured model after an HTTP "
    "429 fallback) are real measured values from the provider's own "
    "response where available; a record falls back to an output-length "
    "estimate only when the provider didn't return one — see each record's "
    "completion_tokens_is_estimate flag, and model_actual_breakdown below "
    "for the live model mix. Cost is computed per record using that "
    "record's own actual (or configured, if unmeasured) model against "
    "pricing.json's rate table, not a single blended rate."
)

_EPHEMERALITY_NOTE = (
    "Under Decision Gate D1 Option B, usage records are stored in Postgres "
    "(the llm_usage_events table), not the ephemeral container filesystem — "
    "so unlike the brief's default JSONL design, token/cost/latency history "
    "here DOES survive restarts and deploys, same as session counts."
)

# Sarvam (voice/multilingual) usage is a separate data source from the LLM
# usage above — bills per character, not per token, has its own table
# (sarvam_usage_events) and its own pricing file (sarvam_pricing.json), and
# is reported under its own "voice_usage" key rather than folded into the
# figures above, so the two never get silently blended into one number.
_VOICE_COVERAGE_NOTE = (
    "Covers both Sarvam call sites: POST /api/tts (translate + "
    "text-to-speech, the voice-playback feature — call_site="
    "'tts_proxy_translate'/'tts_proxy_tts') and the patient-answer "
    "translate-back on every non-English turn (call_site="
    "'patient_answer_translate'). Character counts are real (len() of the "
    "text actually sent), not estimated — TTS truncates its input to 500 "
    "characters before synthesis, and the logged char_count reflects that "
    "truncation. Cost is estimated from sarvam_pricing.json's per-1,000-"
    "character list rates, which are NOT verified against actual Sarvam "
    "invoices. POST /api/tts does not receive a session_id in its request "
    "body, so those records have session_id=null and are excluded from "
    "per-session figures — only counted in the aggregate totals below."
)


def _load_pricing() -> dict[str, Any]:
    try:
        with open(_PRICING_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        _log.warning("Could not load pricing.json at %s", _PRICING_PATH, exc_info=True)
        return {"_default": {"input_usd_per_1k": 0.0005, "output_usd_per_1k": 0.0015}}


def _load_sarvam_pricing() -> dict[str, Any]:
    try:
        with open(_SARVAM_PRICING_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        _log.warning("Could not load sarvam_pricing.json at %s", _SARVAM_PRICING_PATH, exc_info=True)
        return {
            "translate": {"usd_per_1k_chars": 0.023},
            "tts": {"usd_per_1k_chars": 0.018},
        }


def _sarvam_event_cost(event: "SarvamUsageEvent", pricing: dict) -> float | None:
    if event.char_count is None:
        return None
    rate = pricing.get(event.operation, {}).get("usd_per_1k_chars")
    if rate is None:
        return None
    return event.char_count / 1000 * rate


def _sarvam_usage_summary(events: list["SarvamUsageEvent"], days: int) -> dict[str, Any]:
    """Aggregate Sarvam usage over the window into the shape the admin
    portal renders — request/char/cost totals, plus breakdowns by operation
    (translate vs tts) and by target language."""
    if not events:
        return {
            "status": "no_data",
            "message": (
                "No voice/translation usage yet for source='live_web' in this window — "
                "accumulates once a session uses a non-English language or plays back "
                "voice audio."
            ),
            "window_days": days,
            "coverage_note": _VOICE_COVERAGE_NOTE,
        }

    pricing = _load_sarvam_pricing()
    event_costs = [_sarvam_event_cost(e, pricing) for e in events]
    sum_cost = sum(c for c in event_costs if c is not None)

    by_op: dict[str, dict[str, Any]] = {}
    by_lang: dict[str, dict[str, Any]] = {}
    ok_count = 0
    for e, cost in zip(events, event_costs):
        if e.ok:
            ok_count += 1
        op_row = by_op.setdefault(e.operation, {"operation": e.operation, "count": 0, "chars": 0, "cost_usd_estimated": 0.0})
        op_row["count"] += 1
        op_row["chars"] += e.char_count or 0
        op_row["cost_usd_estimated"] += cost or 0.0
        if e.language:
            lang_row = by_lang.setdefault(e.language, {"language": e.language, "count": 0, "chars": 0})
            lang_row["count"] += 1
            lang_row["chars"] += e.char_count or 0

    return {
        "status": "ok",
        "window_days": days,
        "requests_total": len(events),
        "requests_ok": ok_count,
        "requests_failed": len(events) - ok_count,
        "chars_total": sum(e.char_count or 0 for e in events),
        "cost_usd_estimated": round(sum_cost, 6),
        "by_operation": [
            {**row, "cost_usd_estimated": round(row["cost_usd_estimated"], 6)}
            for row in sorted(by_op.values(), key=lambda r: -r["count"])
        ],
        "by_language": sorted(by_lang.values(), key=lambda r: -r["count"]),
        "coverage_note": _VOICE_COVERAGE_NOTE,
    }


def _rate_for_model(pricing: dict, model: str | None) -> dict:
    if model and model in pricing:
        return pricing[model]
    return pricing["_default"]


def _estimate_cost(prompt_tokens: float | None, completion_tokens: float | None, rates: dict) -> float | None:
    if prompt_tokens is None or completion_tokens is None:
        return None
    return (prompt_tokens / 1000 * rates["input_usd_per_1k"]) + (completion_tokens / 1000 * rates["output_usd_per_1k"])


def _event_cost(event: LlmUsageEvent, pricing: dict) -> float | None:
    """Cost for one usage record, priced against the model that actually
    served it (falling back to the configured model when model_actual is
    unmeasured — an older record, or a call site not yet upgraded). Priced
    per-event rather than with one blended rate, because doctor/compressor/
    critic calls can each use a different model with a different price."""
    model_for_pricing = event.model_actual or event.model_configured
    rates = _rate_for_model(pricing, model_for_pricing)
    return _estimate_cost(event.prompt_tokens, event.completion_tokens_estimated, rates)


_CALL_SITE_ROLE = {
    "initialize": "doctor",
    "next_question": "doctor",
    "final_generation": "doctor",
    "compressor": "compressor",
    "profile_update": "profile_updater",
    "critic": "critic",
    "closing_turn": "closing_turn",
}


def _model_actual_breakdown_by_role(events: list[LlmUsageEvent]) -> dict[str, list[dict[str, Any]]]:
    """Same computation as _model_actual_breakdown, but grouped by role
    (doctor/critic/compressor/closing_turn) first. The flat, all-roles-mixed
    breakdown makes it impossible to tell "the critic is actually being
    served by model X" from "the doctor is" when they differ — this answers
    that per-role, so the admin portal can show it next to each role's
    configured model instead of one blended line."""
    by_role: dict[str, list[LlmUsageEvent]] = {}
    for e in events:
        role = _CALL_SITE_ROLE.get(e.call_site, e.call_site)
        by_role.setdefault(role, []).append(e)
    return {role: _model_actual_breakdown(evs)[0] for role, evs in by_role.items()}


def _model_actual_breakdown(events: list[LlmUsageEvent]) -> tuple[list[dict[str, Any]], int]:
    """Live model mix actually serving requests (post-fallback), grouped
    from model_actual. Returns (breakdown, records_missing_model_actual) —
    the latter counts records logged before this field existed, so the
    caller can disclose them rather than silently drop them from the
    percentages."""
    counts: dict[str, int] = {}
    missing = 0
    for e in events:
        if e.model_actual:
            counts[e.model_actual] = counts.get(e.model_actual, 0) + 1
        else:
            missing += 1
    known_total = len(events) - missing
    breakdown = [
        {
            "model": model,
            "count": count,
            "pct": round(count / known_total * 100, 1) if known_total else None,
        }
        for model, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    return breakdown, missing


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

        # --- Separate source: sarvam_usage_events (voice/translation,
        # character-billed — see _sarvam_usage_summary) ---
        sarvam_all_in_window = db.execute(
            select(SarvamUsageEvent).where(SarvamUsageEvent.recorded_at >= window_start)
        ).scalars().all()

    sarvam_live_events = [e for e in sarvam_all_in_window if e.source == _DEFAULT_SOURCE]
    voice_usage = _sarvam_usage_summary(sarvam_live_events, days)

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
            "voice_usage": voice_usage,
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
    # Priced per-event against each record's own actual (or configured)
    # model — doctor/compressor/critic calls can each be a different model
    # with a different rate, so one blended rate would misprice the mix.
    event_costs = [_event_cost(e, pricing) for e in live_events]
    sum_cost = sum(c for c in event_costs if c is not None)
    cost_per_session = sum_cost / n_usage_sessions if n_usage_sessions else None

    model_actual_breakdown, model_actual_unknown_count = _model_actual_breakdown(live_events)
    model_actual_breakdown_by_role = _model_actual_breakdown_by_role(live_events)

    daily: list[dict[str, Any]] = []
    events_by_day: dict[date, list[LlmUsageEvent]] = {}
    for e in live_events:
        d = e.recorded_at.date() if hasattr(e.recorded_at, "date") else e.recorded_at
        events_by_day.setdefault(d, []).append(e)

    for i in range(days):
        d = (now - timedelta(days=days - 1 - i)).date()
        day_events = events_by_day.get(d, [])
        day_costs = [_event_cost(e, pricing) for e in day_events]
        day_cost = sum(c for c in day_costs if c is not None)
        daily.append({
            "date": d.isoformat(),
            "sessions": sessions_by_day.get(d, 0),
            "turns": len(day_events),
            "cost_usd_estimated": round(day_cost, 6) if day_cost is not None else 0.0,
        })

    model_actual_note = None
    if model_actual_unknown_count:
        model_actual_note = (
            f"{model_actual_unknown_count} of {usage_records_total} usage record(s) in this "
            "window have no model_actual — logged before this field was measured, or from a "
            "call site not yet upgraded. Excluded from the percentages above, not silently "
            "folded into any single model's share."
        )

    return {
        "status": "ok",
        "window_days": days,
        "cost_per_session_usd_estimated": round(cost_per_session, 6) if cost_per_session is not None else None,
        "mean_tokens_per_session": round(mean_tokens_per_session, 1),
        "p95_llm_latency_ms": round(p95_latency, 1) if p95_latency is not None else None,
        "sessions_today": sessions_today,
        "daily": daily,
        "model_actual_breakdown": model_actual_breakdown,
        "model_actual_breakdown_by_role": model_actual_breakdown_by_role,
        "voice_usage": voice_usage,
        "counts": {
            "usage_records": usage_records_total,
            "sessions_in_window": sessions_in_window,
            "records_excluded_by_source_filter": excluded_by_source,
            "model_actual_unknown": model_actual_unknown_count,
            # Always 0 under Decision Gate D1 Option B (Postgres) — the
            # brief's "malformed_lines_skipped" count is a JSONL-parsing
            # concept (Option A); a DB row either matches the schema or
            # doesn't exist. Field kept for contract compatibility.
            "malformed_lines_skipped": 0,
        },
        "coverage_note": _COVERAGE_NOTE,
        "notes": [_EPHEMERALITY_NOTE] + ([p95_note] if p95_note else []) + ([model_actual_note] if model_actual_note else []),
    }
