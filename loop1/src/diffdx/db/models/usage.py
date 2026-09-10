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
    # Always NULL today, deliberately: loop1.llm's _FALLBACK_CHAIN can serve
    # a request from a different provider on HTTP 429 and does not report
    # which one. Column exists so it can be filled in if llm.py ever
    # returns that; omitting it entirely would hide the gap.
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
