from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from diffdx.db.base import Base
from diffdx.db.types import GUID


class MessageReport(Base):
    """A patient or doctor flagging the other side of a message thread for
    review — e.g. using the channel for something other than medical care.
    Purely a human-reviewed queue (same shape as DsrErasureRequest): no
    automated action is taken on submission, an admin reviews and marks it
    reviewed/dismissed via admin_portal/routers/reports.py.

    reported_doctor_id is the legacy short doctor id ("dr_001"), not a
    users.id UUID — same reason messaging.py's own message rows never FK
    doctor_id either (see routers/messaging.py). Names are denormalized at
    creation time for the same reason messages.py denormalizes
    patient_name/doctor_name onto every message row: the messaging domain
    itself is still blob-based, not relationally joinable here.
    """

    __tablename__ = "message_reports"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    reporter_user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reporter_role: Mapped[str] = mapped_column(String(20), nullable=False)
    reporter_name: Mapped[str] = mapped_column(String(200), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    reported_patient_user_id: Mapped[str | None] = mapped_column(String(64))
    reported_doctor_id: Mapped[str | None] = mapped_column(String(50))
    reported_name: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="open")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column()
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL")
    )
    admin_note: Mapped[str | None] = mapped_column(Text)
