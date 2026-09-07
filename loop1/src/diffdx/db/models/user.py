from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, Index, Integer, String, func, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from diffdx.db.base import Base
from diffdx.db.types import GUID

UserRole = Enum("patient", "doctor", "admin", name="user_role")


class User(Base):
    """Single identity table. Patient/Doctor hang off this via 1:1 FK."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(UserRole, nullable=False, server_default="patient")
    # Doctors are always seeded (scripts/seed_doctors.py), never self-registered,
    # so they default verified. Patient self-registration explicitly passes
    # False and clears it via the OTP flow (diffdx.otp) before the account
    # can log in — see UserRepository.create_patient's email_verified param.
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped["Patient | None"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")
    doctor: Mapped["Doctor | None"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    email_otp: Mapped["EmailOtp | None"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        # Case-insensitive uniqueness on email — Postgres uses a functional
        # index (citext isn't assumed available), SQLite falls back to a
        # plain unique index via the same expression at the dialect level
        # in the migration (see alembic/versions).
        Index("uq_users_email_lower", func.lower(email), unique=True),
    )


class Patient(Base):
    """Patient-only profile fields, 1:1 with User."""

    __tablename__ = "patients"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    mobile: Mapped[str | None] = mapped_column(String(30))
    age: Mapped[int | None]
    blood_type: Mapped[str | None] = mapped_column(String(10))
    gender: Mapped[str | None] = mapped_column(String(30))
    address: Mapped[str | None] = mapped_column(String(500))
    emergency_contact_name: Mapped[str | None] = mapped_column(String(200))
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(30))
    allergies: Mapped[str | None] = mapped_column(String(2000))
    chronic_conditions: Mapped[str | None] = mapped_column(String(2000))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="patient")
    dependents: Mapped[list["Dependent"]] = relationship(back_populates="patient", cascade="all, delete-orphan")


class Doctor(Base):
    """Doctor-only profile fields, 1:1 with User.

    doctor_id keeps the legacy short id ("dr_001") used throughout the
    existing frontend/doctors.json seed data, distinct from the UUID PK.
    """

    __tablename__ = "doctors"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    doctor_id: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    specialty: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    hospital: Mapped[str | None] = mapped_column(String(200))
    rating: Mapped[float | None]
    avatar_initials: Mapped[str | None] = mapped_column(String(10))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="doctor")
    slots: Mapped[list["DoctorSlot"]] = relationship(back_populates="doctor", cascade="all, delete-orphan")
    blocked_dates: Mapped[list["BlockedDate"]] = relationship(back_populates="doctor", cascade="all, delete-orphan")


class Dependent(Base):
    """A family member managed under a patient's account."""

    __tablename__ = "dependents"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("patients.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    relationship_: Mapped[str] = mapped_column("relationship", String(30), nullable=False)
    age: Mapped[int | None]
    gender: Mapped[str | None] = mapped_column(String(30))
    blood_type: Mapped[str | None] = mapped_column(String(10))
    allergies: Mapped[str | None] = mapped_column(String(2000))
    chronic_conditions: Mapped[str | None] = mapped_column(String(2000))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped[Patient] = relationship(back_populates="dependents")


class RefreshToken(Base):
    """Hashed refresh tokens for Task 5 (JWT auth) — table lives here now so
    Task 5 doesn't need its own schema migration."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")
