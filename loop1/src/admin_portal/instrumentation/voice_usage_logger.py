"""
Sarvam AI (voice/multilingual) usage telemetry — parallel to usage_logger.py
but for the Sarvam translate/text-to-speech calls, which bill per character,
not per token, so they get their own table (diffdx.db.models.usage.
SarvamUsageEvent) rather than being squeezed into LlmUsageEvent's
token-shaped columns.

Contract, matching usage_logger.py's exactly: never raises. A telemetry
failure must not fail a live patient interview or a voice-playback request —
every public function wraps its body in a broad try/except that logs at
warning level and returns.

No PHI is ever logged here: ids, counts, language codes, timings only.
Never the actual text sent to or received from Sarvam.
"""
from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)


def _resolve_source() -> str:
    """'live_web' vs 'offline_cli' — matches usage_logger._resolve_source()
    exactly, same env var, same default."""
    return os.environ.get("DIFFDX_RUN_CONTEXT", "live_web")


def log_sarvam_usage(
    *,
    session_id: str | None,
    operation: str,
    call_site: str,
    language: str | None,
    model: str | None,
    char_count: int | None,
    latency_ms: float | None,
    ok: bool = True,
    error_type: str | None = None,
) -> None:
    """Log one Sarvam API call (translate or text-to-speech). Keyword-only
    so a future signature change can never silently reorder positional
    arguments at a call site.

    Never raises — see module docstring.
    """
    try:
        from diffdx.db.engine import get_sessionmaker
        from diffdx.db.models.usage import SarvamUsageEvent

        session = get_sessionmaker()()
        try:
            session.add(
                SarvamUsageEvent(
                    session_id=str(session_id) if session_id else None,
                    operation=str(operation),
                    call_site=str(call_site),
                    language=language,
                    model=model,
                    char_count=char_count,
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
                "Sarvam usage log write failed for session=%s call_site=%s",
                session_id, call_site, exc_info=True,
            )
        finally:
            session.close()
    except Exception:
        # Catches failures before a DB session even exists (e.g. config or
        # import errors) — still must never raise into the caller.
        _log.warning(
            "log_sarvam_usage failed entirely for session=%s call_site=%s",
            session_id, call_site, exc_info=True,
        )
