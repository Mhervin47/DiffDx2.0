from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from diffdx.db.models.user import Dependent, Doctor, Patient, RefreshToken, User
from diffdx.db.models.verification import EmailOtp


@dataclass(frozen=True, slots=True)
class UserDTO:
    id: uuid.UUID
    name: str
    email: str
    password_hash: str
    role: str
    created_at: datetime
    email_verified: bool = True
    # Patient-only fields (None for doctors)
    mobile: str | None = None
    age: int | None = None
    blood_type: str | None = None
    gender: str | None = None
    address: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    allergies: str | None = None
    chronic_conditions: str | None = None
    # Doctor-only fields (None for patients)
    doctor_id: str | None = None
    specialty: str | None = None
    hospital: str | None = None
    rating: float | None = None
    avatar_initials: str | None = None


@dataclass(frozen=True, slots=True)
class DependentDTO:
    id: uuid.UUID
    patient_id: uuid.UUID
    name: str
    relationship: str
    age: int | None = None
    gender: str | None = None
    blood_type: str | None = None
    allergies: str | None = None
    chronic_conditions: str | None = None


def _to_user_dto(user: User) -> UserDTO:
    patient, doctor = user.patient, user.doctor
    return UserDTO(
        id=user.id,
        name=user.name,
        email=user.email,
        password_hash=user.password_hash,
        role=user.role,
        created_at=user.created_at,
        email_verified=user.email_verified,
        mobile=patient.mobile if patient else None,
        age=patient.age if patient else None,
        blood_type=patient.blood_type if patient else None,
        gender=patient.gender if patient else None,
        address=patient.address if patient else None,
        emergency_contact_name=patient.emergency_contact_name if patient else None,
        emergency_contact_phone=patient.emergency_contact_phone if patient else None,
        allergies=patient.allergies if patient else None,
        chronic_conditions=patient.chronic_conditions if patient else None,
        doctor_id=doctor.doctor_id if doctor else None,
        specialty=doctor.specialty if doctor else None,
        hospital=doctor.hospital if doctor else None,
        rating=doctor.rating if doctor else None,
        avatar_initials=doctor.avatar_initials if doctor else None,
    )


def _to_dependent_dto(dep: Dependent) -> DependentDTO:
    return DependentDTO(
        id=dep.id,
        patient_id=dep.patient_id,
        name=dep.name,
        relationship=dep.relationship_,
        age=dep.age,
        gender=dep.gender,
        blood_type=dep.blood_type,
        allergies=dep.allergies,
        chronic_conditions=dep.chronic_conditions,
    )


class UserRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, user_id: uuid.UUID) -> UserDTO | None:
        user = self._session.get(User, user_id)
        return _to_user_dto(user) if user else None

    def get_by_email(self, email: str) -> UserDTO | None:
        stmt = select(User).where(func.lower(User.email) == email.lower().strip())
        user = self._session.execute(stmt).scalar_one_or_none()
        return _to_user_dto(user) if user else None

    def list_all(self) -> list[UserDTO]:
        return [_to_user_dto(u) for u in self._session.execute(select(User)).scalars()]

    def get_by_doctor_id(self, doctor_id: str) -> UserDTO | None:
        stmt = select(User).join(Doctor, Doctor.user_id == User.id).where(Doctor.doctor_id == doctor_id)
        user = self._session.execute(stmt).scalar_one_or_none()
        return _to_user_dto(user) if user else None

    def create_patient(
        self,
        *,
        name: str,
        email: str,
        password_hash: str,
        id: uuid.UUID | None = None,
        created_at: datetime | None = None,
        patient_fields: dict | None = None,
        email_verified: bool = True,
    ) -> UserDTO:
        user = User(
            id=id or uuid.uuid4(),
            name=name,
            email=email.lower().strip(),
            password_hash=password_hash,
            role="patient",
            email_verified=email_verified,
        )
        if created_at is not None:
            user.created_at = created_at
        user.patient = Patient(user_id=user.id, **(patient_fields or {}))
        self._session.add(user)
        self._session.flush()
        return _to_user_dto(user)

    def create_doctor(
        self,
        *,
        name: str,
        email: str,
        password_hash: str,
        doctor_id: str,
        specialty: str,
        id: uuid.UUID | None = None,
        created_at: datetime | None = None,
        hospital: str | None = None,
        rating: float | None = None,
        avatar_initials: str | None = None,
    ) -> UserDTO:
        user = User(
            id=id or uuid.uuid4(),
            name=name,
            email=email.lower().strip(),
            password_hash=password_hash,
            role="doctor",
            email_verified=True,
        )
        if created_at is not None:
            user.created_at = created_at
        user.doctor = Doctor(
            user_id=user.id,
            doctor_id=doctor_id,
            specialty=specialty,
            hospital=hospital,
            rating=rating,
            avatar_initials=avatar_initials,
        )
        self._session.add(user)
        self._session.flush()
        return _to_user_dto(user)

    def add_dependent(
        self,
        patient_id: uuid.UUID,
        *,
        name: str,
        relationship: str,
        id: uuid.UUID | None = None,
        age: int | None = None,
        gender: str | None = None,
        blood_type: str | None = None,
        allergies: str | None = None,
        chronic_conditions: str | None = None,
    ) -> DependentDTO:
        dep = Dependent(
            id=id or uuid.uuid4(),
            patient_id=patient_id,
            name=name,
            relationship_=relationship,
            age=age,
            gender=gender,
            blood_type=blood_type,
            allergies=allergies,
            chronic_conditions=chronic_conditions,
        )
        self._session.add(dep)
        self._session.flush()
        return _to_dependent_dto(dep)

    def list_dependents(self, patient_id: uuid.UUID) -> list[DependentDTO]:
        stmt = select(Dependent).where(Dependent.patient_id == patient_id)
        return [_to_dependent_dto(d) for d in self._session.execute(stmt).scalars()]

    def update_patient(self, user_id: uuid.UUID, *, name: str | None = None, **patient_fields) -> UserDTO | None:
        """Partial update: only keys actually passed in `patient_fields` are
        applied (no null-clobber of a field the caller didn't mean to touch).
        `name` lives on `User`, not `Patient`, so it's a separate kwarg."""
        user = self._session.get(User, user_id)
        if user is None:
            return None
        if name is not None:
            user.name = name
        if user.patient is not None:
            for key, value in patient_fields.items():
                setattr(user.patient, key, value)
        self._session.flush()
        return _to_user_dto(user)

    def update_doctor(self, user_id: uuid.UUID, *, name: str | None = None, **doctor_fields) -> UserDTO | None:
        """Same partial-update contract as update_patient, for Doctor columns
        (specialty, hospital, rating, avatar_initials)."""
        user = self._session.get(User, user_id)
        if user is None:
            return None
        if name is not None:
            user.name = name
        if user.doctor is not None:
            for key, value in doctor_fields.items():
                setattr(user.doctor, key, value)
        self._session.flush()
        return _to_user_dto(user)

    def update_password(self, user_id: uuid.UUID, password_hash: str) -> None:
        user = self._session.get(User, user_id)
        if user is not None:
            user.password_hash = password_hash
            self._session.flush()

    def update_dependent(self, dependent_id: uuid.UUID, **fields) -> DependentDTO | None:
        dep = self._session.get(Dependent, dependent_id)
        if dep is None:
            return None
        for key, value in fields.items():
            setattr(dep, "relationship_" if key == "relationship" else key, value)
        self._session.flush()
        return _to_dependent_dto(dep)

    def delete_dependent(self, dependent_id: uuid.UUID) -> bool:
        dep = self._session.get(Dependent, dependent_id)
        if dep is None:
            return False
        self._session.delete(dep)
        self._session.flush()
        return True

    def shadow_user(
        self,
        *,
        id: uuid.UUID,
        name: str,
        email: str,
        password_hash: str,
        role: str,
    ) -> None:
        """Upsert a bare User row (no Patient/Doctor sub-profile) with the
        given primary key.

        Originally Task 5 scaffolding for when user identity still lived
        in the legacy blob store — no longer called by
        routers/auth.py::_issue_token_pair since the identity cutover
        (users/patients/doctors/dependents are real relational data now,
        `UserRepository.create_patient`/`create_doctor` create the real
        row directly). Still called by `diffdx.audit.log_audit_event` on
        every audited action, and kept as a cheap safety-net method (it
        only ever touches `User` columns, never `Patient`/`Doctor`, so it
        can't clobber profile data) — not removed in case any caller still
        needs to guarantee a bare row exists before the FK-dependent
        insert that follows it. `Session.merge` does the
        insert-if-absent/update-if-present logic in one call.
        """
        self._session.merge(User(id=id, name=name, email=email.lower().strip(), password_hash=password_hash, role=role))
        self._session.flush()

    def mark_email_verified(self, user_id: uuid.UUID) -> None:
        user = self._session.get(User, user_id)
        if user is not None:
            user.email_verified = True
            self._session.flush()


@dataclass(frozen=True, slots=True)
class RefreshTokenDTO:
    id: uuid.UUID
    user_id: uuid.UUID
    token_hash: str
    expires_at: datetime
    revoked_at: datetime | None


def _to_refresh_token_dto(rt: RefreshToken) -> RefreshTokenDTO:
    return RefreshTokenDTO(
        id=rt.id,
        user_id=rt.user_id,
        token_hash=rt.token_hash,
        expires_at=rt.expires_at,
        revoked_at=rt.revoked_at,
    )


class RefreshTokenRepository:
    """Hashed, revocable, rotatable refresh tokens (Task 5). Only the
    SHA-256 hash of a refresh token is ever stored — see
    diffdx.auth_tokens.hash_refresh_token — so a DB dump alone can't be
    used to mint sessions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, *, user_id: uuid.UUID, token_hash: str, expires_at: datetime) -> RefreshTokenDTO:
        rt = RefreshToken(id=uuid.uuid4(), user_id=user_id, token_hash=token_hash, expires_at=expires_at)
        self._session.add(rt)
        self._session.flush()
        return _to_refresh_token_dto(rt)

    def get_by_hash(self, token_hash: str) -> RefreshTokenDTO | None:
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        rt = self._session.execute(stmt).scalar_one_or_none()
        return _to_refresh_token_dto(rt) if rt else None

    def revoke(self, token_id: uuid.UUID) -> None:
        rt = self._session.get(RefreshToken, token_id)
        if rt is not None and rt.revoked_at is None:
            rt.revoked_at = datetime.now(timezone.utc)
            self._session.flush()

    def revoke_all_for_user(self, user_id: uuid.UUID) -> None:
        stmt = select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        now = datetime.now(timezone.utc)
        for rt in self._session.execute(stmt).scalars():
            rt.revoked_at = now


class EmailOtpRepository:
    """One pending code per user — issuing a new one (register/resend)
    overwrites whatever was there, so an old, already-sent code stops
    being valid the moment a new one is requested."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, *, user_id: uuid.UUID, code_hash: str, expires_at: datetime) -> None:
        existing = self._session.get(EmailOtp, user_id)
        if existing is None:
            self._session.add(EmailOtp(user_id=user_id, code_hash=code_hash, expires_at=expires_at, attempts=0))
        else:
            existing.code_hash = code_hash
            existing.expires_at = expires_at
            existing.attempts = 0
        self._session.flush()

    def get(self, user_id: uuid.UUID) -> EmailOtp | None:
        return self._session.get(EmailOtp, user_id)

    def increment_attempts(self, user_id: uuid.UUID) -> int:
        otp = self._session.get(EmailOtp, user_id)
        if otp is None:
            return 0
        otp.attempts += 1
        self._session.flush()
        return otp.attempts

    def delete(self, user_id: uuid.UUID) -> None:
        otp = self._session.get(EmailOtp, user_id)
        if otp is not None:
            self._session.delete(otp)
            self._session.flush()
        self._session.flush()
