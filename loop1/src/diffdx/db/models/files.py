from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from diffdx.db.base import Base
from diffdx.db.types import GUID


class UploadedFile(Base):
    """Metadata for a patient/doctor-uploaded file. The bytes themselves live
    on disk under web/data/files/ (see Task 2 migration) — only the path is
    stored here, never base64 content in the DB."""

    __tablename__ = "uploaded_files"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("appointments.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str | None] = mapped_column(String(36), index=True)
    suggested_test_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("suggested_tests.id", ondelete="SET NULL")
    )

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(200))

    uploaded_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    appointment: Mapped["Appointment | None"] = relationship()
