from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from diffdx.db.base import Base
from diffdx.db.types import GUID


class DsrErasureRequest(Base):
    """A patient's in-app request that their data be deleted — the queue
    an admin works from before running the existing erase flow in
    admin_portal/routers/dsr.py. This table does not itself perform any
    erasure; "approving" a request is just running that unmodified flow.

    Deliberately only two statuses ("pending" / "denied"), not three.
    user_id is ON DELETE CASCADE: when an admin actually erases the
    subject, this row is removed automatically by the database as part of
    that same transaction, alongside Patient/Appointments/etc. — no
    separate "mark completed" step, and no risk of a request row
    surviving with a stale status after the user it refers to is gone
    (see admin_portal/routers/dsr.py's own docstring on exactly this
    class of "looks erased but isn't" failure). The durable record that
    an erasure happened is the pseudonymised audit_log_entries row
    (action="subject_erasure") that flow already writes — this table
    doesn't need to duplicate that; "completed" just means the row is no
    longer here.
    """

    __tablename__ = "dsr_erasure_requests"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    reason: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column()
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL")
    )
    admin_note: Mapped[str | None] = mapped_column(Text)
