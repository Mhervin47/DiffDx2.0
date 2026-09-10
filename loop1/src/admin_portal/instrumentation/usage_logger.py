"""
Live-session usage telemetry — admin portal, Phase 2, Decision Gate D1
Option B (Postgres, not JSONL — durable across restarts/deploys, unlike the
ephemeral container filesystem every log/ and data/ path in this repo lives
on).

Writes one row to diffdx.db.models.usage.LlmUsageEvent per doctor-model
call. No FK to diagnostic_sessions (see that model's docstring for why).

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
    object's repr) whenever doctor_output isn't a real pydantic model —
    loop1.llm never returns a completion-token count for any model call in
    this codebase, so this has always been an estimate, and an unavailable
    estimate is unmeasured, not a number derived from junk input."""
    if doctor_output is None or not hasattr(doctor_output, "model_dump"):
        return None
    try:
        return len(json.dumps(doctor_output.model_dump(), default=str)) // 4
    except Exception:
        return None


def log_turn_usage(
    *,
    session_id: str,
    turn_index: int,
    call_site: str,
    prompt_tokens: int | None,
    doctor_output: Any,
    latency_ms: float | None,
    ok: bool = True,
    error_type: str | None = None,
) -> None:
    """Log one doctor-model call. Keyword-only so a future signature change
    can never silently reorder positional arguments at a call site.

    Never raises — see module docstring.
    """
    try:
        from loop1.config import config
        from diffdx.db.engine import get_sessionmaker
        from diffdx.db.models.usage import LlmUsageEvent

        completion_tokens_estimated = _estimate_completion_tokens(doctor_output)
        try:
            model_configured = config["models"]["doctor"]
        except Exception:
            model_configured = None

        session = get_sessionmaker()()
        try:
            session.add(
                LlmUsageEvent(
                    session_id=str(session_id),
                    turn_index=int(turn_index),
                    call_site=str(call_site),
                    model_configured=model_configured,
                    model_actual=None,  # always null — see LlmUsageEvent's own docstring
                    prompt_tokens=prompt_tokens,
                    completion_tokens_estimated=completion_tokens_estimated,
                    completion_tokens_is_estimate=True,
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
