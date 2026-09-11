"""
Live-session usage telemetry — admin portal, Phase 2, Decision Gate D1
Option B (Postgres, not JSONL — durable across restarts/deploys, unlike the
ephemeral container filesystem every log/ and data/ path in this repo lives
on).

Writes one row to diffdx.db.models.usage.LlmUsageEvent per LLM call —
doctor, compressor, profile-update, or critic (Phase 3's Item 12 extension;
call_site distinguishes them, no schema change needed). No FK to
diagnostic_sessions (see that model's docstring for why).

Contract, matching diffdx.audit.log_audit_event's existing one exactly:
never raises. A telemetry failure must not fail a live patient interview —
every public function wraps its body in a broad try/except that logs at
warning level and returns.

No PHI is ever logged here: ids, counts, timings, and model names only.
Never prompt text, patient answers, rationale, or differential contents.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

_log = logging.getLogger(__name__)


def _resolve_source() -> str:
    """'live_web' (api_session.py) vs 'offline_cli' (session.py, deferred
    Task 9) — so phase7_eval.py / CLI runs never pollute production
    metrics unless the caller explicitly opts in via this env var."""
    return os.environ.get("DIFFDX_RUN_CONTEXT", "live_web")


def _estimate_completion_tokens(doctor_output: Any) -> int | None:
    """Phase 1's convention: len(json.dumps(doctor_output.model_dump())) // 4.
    Returns None (not 0, and not a meaningless estimate from some other
    object's repr) whenever doctor_output isn't a real pydantic model. Only
    used as a fallback now — Phase 3 threads the real completion_tokens
    through from the provider's response when call_llm_with_usage() is the
    caller, so this estimate only fires when that value is unavailable
    (e.g. the provider omitted it, or a call site hasn't been upgraded)."""
    if doctor_output is None or not hasattr(doctor_output, "model_dump"):
        return None
    try:
        return len(json.dumps(doctor_output.model_dump(), default=str)) // 4
    except Exception:
        return None


def _resolve_model_configured(call_site: str) -> str | None:
    """Which config entry describes this call_site's *intended* model — not
    necessarily what actually served it (that's model_actual). Each call
    site reads a different config key, so this must not just always return
    config["models"]["doctor"] once compressor/profile_update/critic rows
    exist too, or their configured-model field would silently lie."""
    try:
        from loop1.config import config

        if call_site in ("initialize", "next_question", "final_generation"):
            return config["models"]["doctor"]
        if call_site == "compressor":
            return config["models"]["compressor"]
        if call_site == "profile_update":
            return config["models"]["profile_updater"]
        if call_site == "critic":
            from loop2.critic.critic import _critic_model
            return _critic_model()
    except Exception:
        return None
    return None


def log_turn_usage(
    *,
    session_id: str,
    turn_index: int,
    call_site: str,
    prompt_tokens: int | None,
    doctor_output: Any,
    latency_ms: float | None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    model_actual: str | None = None,
    ok: bool = True,
    error_type: str | None = None,
) -> None:
    """Log one LLM call (doctor turn, compressor, profile update, or
    critic — see call_site). Keyword-only so a future signature change can
    never silently reorder positional arguments at a call site.

    completion_tokens/total_tokens/model_actual are real, measured values
    when the caller has them (every call site now goes through
    call_llm_with_usage(), which returns them from the provider's own
    response — see loop1.llm.LlmUsage). When completion_tokens is None,
    this falls back to _estimate_completion_tokens(doctor_output) and marks
    the row as estimated; total_tokens has no column of its own (it's
    trivially prompt + completion wherever needed, and adding one would
    require a migration this change doesn't need).

    Never raises — see module docstring.
    """
    try:
        from diffdx.db.engine import get_sessionmaker
        from diffdx.db.models.usage import LlmUsageEvent

        is_estimate = completion_tokens is None
        completion_tokens_value = (
            completion_tokens if not is_estimate else _estimate_completion_tokens(doctor_output)
        )
        model_configured = _resolve_model_configured(call_site)

        session = get_sessionmaker()()
        try:
            session.add(
                LlmUsageEvent(
                    session_id=str(session_id),
                    turn_index=int(turn_index),
                    call_site=str(call_site),
                    model_configured=model_configured,
                    model_actual=model_actual,
                    prompt_tokens=prompt_tokens,
                    completion_tokens_estimated=completion_tokens_value,
                    completion_tokens_is_estimate=is_estimate,
                    latency_ms=latency_ms,
                    source=_resolve_source(),
                    ok=bool(ok),
                    error_type=error_type,
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            _log.warning(
                "Usage log write failed for session=%s turn=%s call_site=%s",
                session_id, turn_index, call_site, exc_info=True,
            )
        finally:
            session.close()
    except Exception:
        # Catches failures before a DB session even exists (e.g. config or
        # import errors) — still must never raise into the caller.
        _log.warning(
            "log_turn_usage failed entirely for session=%s turn=%s", session_id, turn_index,
            exc_info=True,
        )
