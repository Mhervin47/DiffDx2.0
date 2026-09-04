from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from diffdx.db.models.scheduling import Appointment
from diffdx.exceptions import ConflictError, NotFoundError


@dataclass(frozen=True, slots=True)
class AppointmentDTO:
    id: uuid.UUID
    patient_id: uuid.UUID
    doctor_id: uuid.UUID
    slot_datetime: datetime
    status: str
    urgency: str
    booked_at: datetime
    session_id: str | None = None
    cancelled_at: datetime | None = None
    cancelled_by: str | None = None
    note: str | None = None
    is_followup: bool = False
    parent_appointment_id: uuid.UUID | None = None
    rescheduled_from_id: uuid.UUID | None = None
    chief_complaint: str | None = None
    primary_diagnosis: str | None = None
    patient_age: int | None = None
    patient_sex: str | None = None
    patient_bmi: float | None = None
    rating_stars: int | None = None
    rating_comment: str | None = None
    rating_submitted_at: datetime | None = None


def _to_dto(appt: Appointment) -> AppointmentDTO:
    return AppointmentDTO(
        id=appt.id,
        patient_id=appt.patient_id,
        doctor_id=appt.doctor_id,
        slot_datetime=appt.slot_datetime,
        status=appt.status,
        urgency=appt.urgency,
        booked_at=appt.booked_at,
        session_id=appt.session_id,
        cancelled_at=appt.cancelled_at,
        cancelled_by=appt.cancelled_by,
        note=appt.note,
        is_followup=appt.is_followup,
        parent_appointment_id=appt.parent_appointment_id,
        rescheduled_from_id=appt.rescheduled_from_id,
        chief_complaint=appt.chief_complaint,
        primary_diagnosis=appt.primary_diagnosis,
        patient_age=appt.patient_age,
        patient_sex=appt.patient_sex,
        patient_bmi=appt.patient_bmi,
        rating_stars=appt.rating_stars,
        rating_comment=appt.rating_comment,
        rating_submitted_at=appt.rating_submitted_at,
    )


class AppointmentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, appointment_id: uuid.UUID) -> AppointmentDTO | None:
        appt = self._session.get(Appointment, appointment_id)
        return _to_dto(appt) if appt else None

    def list_for_patient(self, patient_id: uuid.UUID) -> list[AppointmentDTO]:
        stmt = select(Appointment).where(Appointment.patient_id == patient_id)
        return [_to_dto(a) for a in self._session.execute(stmt).scalars()]

    def list_for_doctor(self, doctor_id: uuid.UUID) -> list[AppointmentDTO]:
        stmt = select(Appointment).where(Appointment.doctor_id == doctor_id)
        return [_to_dto(a) for a in self._session.execute(stmt).scalars()]

    def book(
        self,
        *,
        patient_id: uuid.UUID,
        doctor_id: uuid.UUID,
        slot_datetime: datetime,
        id: uuid.UUID | None = None,
        session_id: str | None = None,
        urgency: str = "routine",
        status: str = "upcoming",
        booked_at: datetime | None = None,
        chief_complaint: str | None = None,
        primary_diagnosis: str | None = None,
        patient_age: int | None = None,
        patient_sex: str | None = None,
        patient_bmi: float | None = None,
        note: str | None = None,
        is_followup: bool = False,
        parent_appointment_id: uuid.UUID | None = None,
    ) -> AppointmentDTO:
        """Create a booking. Raises ConflictError if the doctor is already
        booked at this slot (the DB-level UNIQUE (doctor_id, slot_datetime)
        excluding cancelled rows — see Task 1 — is what actually enforces
        this; this just translates the resulting IntegrityError)."""
        appt = Appointment(
            id=id or uuid.uuid4(),
            patient_id=patient_id,
            doctor_id=doctor_id,
            slot_datetime=slot_datetime,
            session_id=session_id,
            urgency=urgency,
            status=status,
            chief_complaint=chief_complaint,
            primary_diagnosis=primary_diagnosis,
            patient_age=patient_age,
            patient_sex=patient_sex,
            patient_bmi=patient_bmi,
            note=note,
            is_followup=is_followup,
            parent_appointment_id=parent_appointment_id,
        )
        if booked_at is not None:
            appt.booked_at = booked_at
        self._session.add(appt)
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise ConflictError(
                "This slot was just booked by someone else.",
                detail=f"Doctor {doctor_id} is already booked at {slot_datetime.isoformat()}.",
            ) from exc
        return _to_dto(appt)

    def cancel(self, appointment_id: uuid.UUID, *, cancelled_by: str, cancelled_at: datetime) -> AppointmentDTO:
        appt = self._session.get(Appointment, appointment_id)
        if appt is None:
            raise NotFoundError(f"Appointment {appointment_id} not found")
        appt.status = "cancelled"
        appt.cancelled_at = cancelled_at
        appt.cancelled_by = cancelled_by
        self._session.flush()
        return _to_dto(appt)

    def set_rating(
        self, appointment_id: uuid.UUID, *, stars: int, comment: str, submitted_at: datetime
    ) -> AppointmentDTO:
        appt = self._session.get(Appointment, appointment_id)
        if appt is None:
            raise NotFoundError(f"Appointment {appointment_id} not found")
        appt.rating_stars = stars
        appt.rating_comment = comment
        appt.rating_submitted_at = submitted_at
        self._session.flush()
        return _to_dto(appt)
