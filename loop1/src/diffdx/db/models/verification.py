from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from diffdx.db.base import Base
from diffdx.db.types import GUID


class EmailOtp(Base):
    """One pending email-verification code per user (registration OTP).

    1:1 with User rather than a log of every code ever issued — resending
    just overwrites code_hash/expires_at/attempts on the same row, since
    only the latest code is ever valid.
    """

    __tablename__ = "email_otps"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    user: Mapped["User"] = relationship(back_populates="email_otp")
