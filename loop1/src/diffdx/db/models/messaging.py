from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from diffdx.db.base import Base
from diffdx.db.types import GUID

SenderRole = Enum("patient", "doctor", name="message_sender_role")


class MessageThread(Base):
    """One thread per (patient, doctor) pair — the legacy code derives this
    grouping at read time from a flat message list; here it's a real row so
    unread counts and thread listing don't require a full table scan."""

    __tablename__ = "message_threads"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("patients.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("doctors.user_id", ondelete="CASCADE"), nullable=False, index=True
    )

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    messages: Mapped[list["Message"]] = relationship(back_populates="thread", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("patient_id", "doctor_id", name="uq_message_threads_patient_id_doctor_id"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("message_threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("appointments.id", ondelete="SET NULL")
    )

    sender_role: Mapped[str] = mapped_column(SenderRole, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False, index=True)
    read: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    thread: Mapped[MessageThread] = relationship(back_populates="messages")
