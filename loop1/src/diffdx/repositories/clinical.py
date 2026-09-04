from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from diffdx.db.models.clinical import (
    Prescription,
    Referral,
    SecondOpinion,
    SuggestedTest,
    TreatmentPlanItem,
)
from diffdx.exceptions import NotFoundError


@dataclass(frozen=True, slots=True)
class SuggestedTestDTO:
    id: uuid.UUID
    appointment_id: uuid.UUID
    test: str
    category: str
    priority: str
    ordered_at: datetime
    notes: str | None = None
    result: str | None = None
    result_status: str | None = None
    result_recorded_at: datetime | None = None


def _to_suggested_test_dto(t: SuggestedTest) -> SuggestedTestDTO:
    return SuggestedTestDTO(
        id=t.id, appointment_id=t.appointment_id, test=t.test, category=t.category,
        priority=t.priority, ordered_at=t.ordered_at, notes=t.notes, result=t.result,
        result_status=t.result_status, result_recorded_at=t.result_recorded_at,
    )


class SuggestedTestRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, appointment_id: uuid.UUID, *, test: str, category: str,
        priority: str = "routine", notes: str | None = None, id: uuid.UUID | None = None,
    ) -> SuggestedTestDTO:
        row = SuggestedTest(
            id=id or uuid.uuid4(), appointment_id=appointment_id,
            test=test, category=category, priority=priority, notes=notes,
        )
        self._session.add(row)
        self._session.flush()
        return _to_suggested_test_dto(row)

    def get_by_id(self, id: uuid.UUID) -> SuggestedTestDTO | None:
        row = self._session.get(SuggestedTest, id)
        return _to_suggested_test_dto(row) if row else None

    def list_for_appointment(self, appointment_id: uuid.UUID) -> list[SuggestedTestDTO]:
        stmt = select(SuggestedTest).where(SuggestedTest.appointment_id == appointment_id)
        return [_to_suggested_test_dto(t) for t in self._session.execute(stmt).scalars()]

    def replace_for_appointment(self, appointment_id: uuid.UUID, tests: list[dict]) -> list[SuggestedTestDTO]:
        """Delete every existing test order for this appointment and insert
        the given list — matches the blob's "PATCH replaces the whole
        test_orders list" semantics (appointments.py::update_test_orders)."""
        self._session.execute(delete(SuggestedTest).where(SuggestedTest.appointment_id == appointment_id))
        self._session.flush()
        created = [
            self.create(
                appointment_id,
                test=t.get("test", ""), category=t.get("category", "other"),
                priority=t.get("priority", "routine"), notes=t.get("notes"),
                id=uuid.UUID(t["id"]) if t.get("id") else None,
            )
            for t in tests
        ]
        return created

    def record_result(
        self, id: uuid.UUID, *, result: str | None, result_status: str | None,
        result_recorded_at: datetime | None = None,
    ) -> SuggestedTestDTO:
        row = self._session.get(SuggestedTest, id)
        if row is None:
            raise NotFoundError(f"SuggestedTest {id} not found")
        row.result = result
        row.result_status = result_status
        row.result_recorded_at = result_recorded_at
        self._session.flush()
        return _to_suggested_test_dto(row)


@dataclass(frozen=True, slots=True)
class PrescriptionDTO:
    id: uuid.UUID
    appointment_id: uuid.UUID
    drug: str
    dose: str
    route: str
    frequency: str
    duration: str
    prescribed_at: datetime
    meal_timing: str | None = None
    notes: str | None = None


def _to_prescription_dto(p: Prescription) -> PrescriptionDTO:
    return PrescriptionDTO(
        id=p.id, appointment_id=p.appointment_id, drug=p.drug, dose=p.dose, route=p.route,
        frequency=p.frequency, duration=p.duration, prescribed_at=p.prescribed_at,
        meal_timing=p.meal_timing, notes=p.notes,
    )


class PrescriptionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, appointment_id: uuid.UUID, *, drug: str, dose: str, frequency: str, duration: str,
        route: str = "oral", meal_timing: str | None = None, notes: str | None = None,
        id: uuid.UUID | None = None,
    ) -> PrescriptionDTO:
        row = Prescription(
            id=id or uuid.uuid4(), appointment_id=appointment_id, drug=drug, dose=dose,
            route=route, frequency=frequency, duration=duration, meal_timing=meal_timing, notes=notes,
        )
        self._session.add(row)
        self._session.flush()
        return _to_prescription_dto(row)

    def list_for_appointment(self, appointment_id: uuid.UUID) -> list[PrescriptionDTO]:
        stmt = select(Prescription).where(Prescription.appointment_id == appointment_id)
        return [_to_prescription_dto(p) for p in self._session.execute(stmt).scalars()]

    def replace_for_appointment(self, appointment_id: uuid.UUID, prescriptions: list[dict]) -> list[PrescriptionDTO]:
        """Same whole-list-replace shape as the blob's `prescriptions` field."""
        self._session.execute(delete(Prescription).where(Prescription.appointment_id == appointment_id))
        self._session.flush()
        return [
            self.create(
                appointment_id,
                drug=p.get("drug", ""), dose=p.get("dose", ""), route=p.get("route", "oral"),
                frequency=p.get("frequency", ""), duration=p.get("duration", ""),
                meal_timing=p.get("meal_timing"), notes=p.get("notes"),
                id=uuid.UUID(p["id"]) if p.get("id") else None,
            )
            for p in prescriptions
        ]


@dataclass(frozen=True, slots=True)
class ReferralDTO:
    id: uuid.UUID
    appointment_id: uuid.UUID
    specialty: str
    urgency: str
    referred_at: datetime
    to_doctor: str | None = None
    notes: str | None = None
    internal_note: str | None = None


def _to_referral_dto(r: Referral) -> ReferralDTO:
    return ReferralDTO(
        id=r.id, appointment_id=r.appointment_id, specialty=r.specialty, urgency=r.urgency,
        referred_at=r.referred_at, to_doctor=r.to_doctor, notes=r.notes, internal_note=r.internal_note,
    )


class ReferralRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self, appointment_id: uuid.UUID, *, specialty: str, to_doctor: str | None = None,
        urgency: str = "Routine", notes: str | None = None, internal_note: str | None = None,
    ) -> ReferralDTO:
        """appointment_id is unique (1:1) and the blob route always overwrites
        the whole referral dict — this is a real upsert, not create-then-update."""
        stmt = select(Referral).where(Referral.appointment_id == appointment_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            row = Referral(id=uuid.uuid4(), appointment_id=appointment_id, specialty=specialty)
            self._session.add(row)
        row.specialty = specialty
        row.to_doctor = to_doctor
        row.urgency = urgency
        row.notes = notes
        row.internal_note = internal_note
        self._session.flush()
        return _to_referral_dto(row)

    def get_for_appointment(self, appointment_id: uuid.UUID) -> ReferralDTO | None:
        stmt = select(Referral).where(Referral.appointment_id == appointment_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return _to_referral_dto(row) if row else None


@dataclass(frozen=True, slots=True)
class TreatmentPlanItemDTO:
    id: uuid.UUID
    appointment_id: uuid.UUID
    text: str
    approved: bool
    source: str
    created_at: datetime


def _to_plan_item_dto(item: TreatmentPlanItem) -> TreatmentPlanItemDTO:
    return TreatmentPlanItemDTO(
        id=item.id, appointment_id=item.appointment_id, text=item.text,
        approved=item.approved, source=item.source, created_at=item.created_at,
    )


class TreatmentPlanItemRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, appointment_id: uuid.UUID, *, text: str, approved: bool = True,
        source: str = "ai", id: uuid.UUID | None = None,
    ) -> TreatmentPlanItemDTO:
        row = TreatmentPlanItem(
            id=id or uuid.uuid4(), appointment_id=appointment_id,
            text=text, approved=approved, source=source,
        )
        self._session.add(row)
        self._session.flush()
        return _to_plan_item_dto(row)

    def list_for_appointment(self, appointment_id: uuid.UUID) -> list[TreatmentPlanItemDTO]:
        stmt = select(TreatmentPlanItem).where(TreatmentPlanItem.appointment_id == appointment_id)
        return [_to_plan_item_dto(item) for item in self._session.execute(stmt).scalars()]

    def replace_for_appointment(self, appointment_id: uuid.UUID, items: list[dict]) -> list[TreatmentPlanItemDTO]:
        """Same whole-list-replace shape as the blob's `approved_plan` field."""
        self._session.execute(delete(TreatmentPlanItem).where(TreatmentPlanItem.appointment_id == appointment_id))
        self._session.flush()
        return [
            self.create(
                appointment_id,
                text=item.get("text", ""), approved=bool(item.get("approved", True)),
                source=item.get("source", "ai"),
                id=uuid.UUID(item["id"]) if item.get("id") else None,
            )
            for item in items
        ]


@dataclass(frozen=True, slots=True)
class SecondOpinionDTO:
    id: uuid.UUID
    appointment_id: uuid.UUID
    from_doctor_id: uuid.UUID
    to_doctor_id: uuid.UUID
    status: str
    requested_at: datetime
    patient_summary: str | None = None
    note: str | None = None
    response: str | None = None
    responded_at: datetime | None = None


def _to_second_opinion_dto(o: SecondOpinion) -> SecondOpinionDTO:
    return SecondOpinionDTO(
        id=o.id, appointment_id=o.appointment_id, from_doctor_id=o.from_doctor_id,
        to_doctor_id=o.to_doctor_id, status=o.status, requested_at=o.requested_at,
        patient_summary=o.patient_summary, note=o.note, response=o.response,
        responded_at=o.responded_at,
    )


class SecondOpinionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, appointment_id: uuid.UUID, from_doctor_id: uuid.UUID, to_doctor_id: uuid.UUID,
        *, patient_summary: str | None = None, note: str | None = None, id: uuid.UUID | None = None,
    ) -> SecondOpinionDTO:
        row = SecondOpinion(
            id=id or uuid.uuid4(), appointment_id=appointment_id,
            from_doctor_id=from_doctor_id, to_doctor_id=to_doctor_id,
            patient_summary=patient_summary, note=note,
        )
        self._session.add(row)
        self._session.flush()
        return _to_second_opinion_dto(row)

    def get_for_appointment(self, appointment_id: uuid.UUID) -> SecondOpinionDTO | None:
        stmt = select(SecondOpinion).where(SecondOpinion.appointment_id == appointment_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return _to_second_opinion_dto(row) if row else None

    def list_inbox_for_doctor(self, to_doctor_id: uuid.UUID) -> list[SecondOpinionDTO]:
        stmt = select(SecondOpinion).where(SecondOpinion.to_doctor_id == to_doctor_id)
        return [_to_second_opinion_dto(o) for o in self._session.execute(stmt).scalars()]

    def respond(self, id: uuid.UUID, *, response: str, responded_at: datetime) -> SecondOpinionDTO:
        row = self._session.get(SecondOpinion, id)
        if row is None:
            raise NotFoundError(f"SecondOpinion {id} not found")
        row.response = response
        row.responded_at = responded_at
        row.status = "responded"
        self._session.flush()
        return _to_second_opinion_dto(row)
