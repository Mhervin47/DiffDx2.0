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
    confirmed_diagnosis: str | None = None
    diagnosis_confirmed_at: datetime | None = None
    patient_age: int | None = None
    patient_sex: str | None = None
    patient_bmi: float | None = None
    rating_stars: int | None = None
    rating_comment: str | None = None
    rating_submitted_at: datetime | None = None
    doctor_summary: str | None = None
    summary_updated_at: datetime | None = None
    doctor_notes: str | None = None
    notes_updated_at: datetime | None = None
    patient_tags: list | None = None
    tags_updated_at: datetime | None = None
    reminder_sent: bool = False


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
        confirmed_diagnosis=appt.confirmed_diagnosis,
        diagnosis_confirmed_at=appt.diagnosis_confirmed_at,
        patient_age=appt.patient_age,
        patient_sex=appt.patient_sex,
        patient_bmi=appt.patient_bmi,
        rating_stars=appt.rating_stars,
        rating_comment=appt.rating_comment,
        rating_submitted_at=appt.rating_submitted_at,
        doctor_summary=appt.doctor_summary,
        summary_updated_at=appt.summary_updated_at,
        doctor_notes=appt.doctor_notes,
        notes_updated_at=appt.notes_updated_at,
        patient_tags=appt.patient_tags,
        tags_updated_at=appt.tags_updated_at,
        reminder_sent=appt.reminder_sent,
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
        rescheduled_from_id: uuid.UUID | None = None,
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
            rescheduled_from_id=rescheduled_from_id,
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

    def update_status(self, appointment_id: uuid.UUID, status: str) -> AppointmentDTO:
        appt = self._session.get(Appointment, appointment_id)
        if appt is None:
            raise NotFoundError(f"Appointment {appointment_id} not found")
        appt.status = status
        self._session.flush()
        return _to_dto(appt)

    def update_summary(self, appointment_id: uuid.UUID, summary: str, *, updated_at: datetime) -> AppointmentDTO:
        appt = self._session.get(Appointment, appointment_id)
        if appt is None:
            raise NotFoundError(f"Appointment {appointment_id} not found")
        appt.doctor_summary = summary
        appt.summary_updated_at = updated_at
        self._session.flush()
        return _to_dto(appt)

    def confirm_diagnosis(
        self, appointment_id: uuid.UUID, diagnosis: str | None, *, confirmed_at: datetime | None
    ) -> AppointmentDTO:
        """Set (or, with diagnosis=None, withdraw) the doctor-confirmed
        diagnosis. Never touches primary_diagnosis — see that column's
        docstring for why they're kept separate."""
        appt = self._session.get(Appointment, appointment_id)
        if appt is None:
            raise NotFoundError(f"Appointment {appointment_id} not found")
        appt.confirmed_diagnosis = diagnosis
        appt.diagnosis_confirmed_at = confirmed_at if diagnosis else None
        self._session.flush()
        return _to_dto(appt)

    def update_notes(self, appointment_id: uuid.UUID, notes: str, *, updated_at: datetime) -> AppointmentDTO:
        appt = self._session.get(Appointment, appointment_id)
        if appt is None:
            raise NotFoundError(f"Appointment {appointment_id} not found")
        appt.doctor_notes = notes
        appt.notes_updated_at = updated_at
        self._session.flush()
        return _to_dto(appt)

    def update_tags(self, appointment_id: uuid.UUID, tags: list, *, updated_at: datetime) -> AppointmentDTO:
        appt = self._session.get(Appointment, appointment_id)
        if appt is None:
            raise NotFoundError(f"Appointment {appointment_id} not found")
        appt.patient_tags = tags
        appt.tags_updated_at = updated_at
        self._session.flush()
        return _to_dto(appt)

    def mark_reminder_sent(self, appointment_id: uuid.UUID) -> AppointmentDTO:
        appt = self._session.get(Appointment, appointment_id)
        if appt is None:
            raise NotFoundError(f"Appointment {appointment_id} not found")
        appt.reminder_sent = True
        self._session.flush()
        return _to_dto(appt)

    def reschedule(self, appointment_id: uuid.UUID, new_slot_datetime: datetime) -> AppointmentDTO:
        """Same conflict semantics as book(): rescheduling into a slot the
        doctor is already booked at (excluding cancelled rows) must be
        rejected the same way booking one is, not silently succeed."""
        appt = self._session.get(Appointment, appointment_id)
        if appt is None:
            raise NotFoundError(f"Appointment {appointment_id} not found")
        doctor_id = appt.doctor_id  # captured before rollback can expire `appt`
        appt.slot_datetime = new_slot_datetime
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise ConflictError(
                "This slot was just booked by someone else.",
                detail=f"Doctor {doctor_id} is already booked at {new_slot_datetime.isoformat()}.",
            ) from exc
        return _to_dto(appt)

    def delete(self, appointment_id: uuid.UUID) -> bool:
        """Permanently remove an appointment and its whole shadow tree
        (every sub-entity table has ondelete="CASCADE" on appointment_id).
        Returns whether a row was actually found and removed — a missing
        id is not an error, just a no-op (mirrors appointments4.py's
        dismiss routes, which permanently remove the blob record and
        must not fail just because no relational shadow ever existed)."""
        appt = self._session.get(Appointment, appointment_id)
        if appt is None:
            return False
        self._session.delete(appt)
        self._session.flush()
        return True
