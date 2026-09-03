from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from diffdx.db.models.user import Dependent, Doctor, Patient, User


@dataclass(frozen=True, slots=True)
class UserDTO:
    id: uuid.UUID
    name: str
    email: str
    password_hash: str
    role: str
    created_at: datetime
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

    def create_patient(
        self,
        *,
        name: str,
        email: str,
        password_hash: str,
        id: uuid.UUID | None = None,
        created_at: datetime | None = None,
        patient_fields: dict | None = None,
    ) -> UserDTO:
        user = User(
            id=id or uuid.uuid4(),
            name=name,
            email=email.lower().strip(),
            password_hash=password_hash,
            role="patient",
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
