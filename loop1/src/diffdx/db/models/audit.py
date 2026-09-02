from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from diffdx.db.base import Base
from diffdx.db.types import GUID


class AuditLogEntry(Base):
    """Append-only. No update or delete paths should ever be written for this
    table — see Task 5 (JWT auth) for what writes here: login, failed login,
    PHI reads, appointment mutations."""

    __tablename__ = "audit_log_entries"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    # RESTRICT, not CASCADE — deleting a user must never delete their audit trail.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[str | None] = mapped_column(String(100))
    resource_id: Mapped[str | None] = mapped_column(String(100))
    ip_address: Mapped[str | None] = mapped_column(String(45))  # IPv6-safe length

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False, index=True)
