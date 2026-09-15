from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from diffdx.db.base import Base
from diffdx.db.types import GUID


class LlmUsageEvent(Base):
    """One row per doctor-model call in the live web session — admin_portal
    Phase 2 (instrumentation/usage_logger.py), Decision Gate D1 Option B.

    No FK to diagnostic_sessions: a turn's usage is logged as it happens,
    but routers/sessions.py only writes a diagnostic_sessions row once the
    session *completes* (_persist_completed_session) — a FK here would fail
    every insert for a session still in progress.
    """

    __tablename__ = "llm_usage_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    call_site: Mapped[str] = mapped_column(String(50), nullable=False)

    model_configured: Mapped[str | None] = mapped_column(String(200))
    # Populated on every successful call: loop1.llm's LlmUsage.model_actual
    # is set to the exact model that answered (post-fallback — _FALLBACK_CHAIN
    # can reroute to a different provider on HTTP 429, and this is how a
    # caller finds out which one actually served the request), and every
    # live call site (doctor, compressor, critic, closing-turn) threads it
    # straight through to log_turn_usage(). NULL only for records logged
    # before this field existed, or a call that failed before completing —
    # see admin_portal_usage.py's model_actual_unknown count for how those
    # are surfaced rather than silently dropped.
    model_actual: Mapped[str | None] = mapped_column(String(200))

    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens_estimated: Mapped[int | None] = mapped_column(Integer)
    completion_tokens_is_estimate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    latency_ms: Mapped[float | None] = mapped_column(Float)

    # "live_web" (api_session.py) vs "offline_cli" (session.py, deferred
    # Task 9) — keeps offline/eval runs from polluting production metrics.
    source: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(200))

    recorded_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False, index=True
    )


class SarvamUsageEvent(Base):
    """One row per Sarvam AI call (translate or text-to-speech) — the voice/
    multilingual feature's usage record. Parallel to LlmUsageEvent but with
    its own table rather than reusing that one's columns: Sarvam bills per
    character, not per token, so prompt_tokens/completion_tokens_estimated
    would be the wrong unit here.

    No FK to diagnostic_sessions, same reasoning as LlmUsageEvent: a call
    can happen before any session row exists at all — POST /api/tts (the
    voice-playback endpoint) doesn't receive a session_id in its request
    body, so session_id is nullable and NULL for that call site.
    """

    __tablename__ = "sarvam_usage_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str | None] = mapped_column(String(36), index=True)

    # "translate" | "tts"
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    # Which call site made the request — "tts_proxy_translate", "tts_proxy_tts",
    # "patient_answer_translate" (see src/diffdx/routers/sessions.py).
    call_site: Mapped[str] = mapped_column(String(50), nullable=False)
    # Sarvam language code, e.g. "hi-IN" — the target language for translate/tts.
    language: Mapped[str | None] = mapped_column(String(10))
    # "mayura:v1" (translate) or "bulbul:v2" (tts) — see legacy_store.py.
    model: Mapped[str | None] = mapped_column(String(50))

    char_count: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[float | None] = mapped_column(Float)

    # "live_web" vs "offline_cli" — matches LlmUsageEvent's convention.
    source: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(200))

    recorded_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False, index=True
    )
