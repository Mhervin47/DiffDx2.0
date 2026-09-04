from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from diffdx.db.models.scheduling import BlockedDate, RescheduleProposal, Waitlist


@dataclass(frozen=True, slots=True)
class WaitlistDTO:
    id: uuid.UUID
    patient_id: uuid.UUID
    doctor_id: uuid.UUID
    status: str
    joined_at: datetime
    note: str | None = None


def _to_waitlist_dto(w: Waitlist) -> WaitlistDTO:
    return WaitlistDTO(
        id=w.id, patient_id=w.patient_id, doctor_id=w.doctor_id,
        status=w.status, joined_at=w.joined_at, note=w.note,
    )


class WaitlistRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def join(
        self, patient_id: uuid.UUID, doctor_id: uuid.UUID, *,
        note: str | None = None, id: uuid.UUID | None = None,
    ) -> WaitlistDTO:
        row = Waitlist(id=id or uuid.uuid4(), patient_id=patient_id, doctor_id=doctor_id, note=note)
        self._session.add(row)
        self._session.flush()
        return _to_waitlist_dto(row)

    def list_for_patient(self, patient_id: uuid.UUID) -> list[WaitlistDTO]:
        stmt = select(Waitlist).where(Waitlist.patient_id == patient_id)
        return [_to_waitlist_dto(w) for w in self._session.execute(stmt).scalars()]

    def list_for_doctor(self, doctor_id: uuid.UUID, status: str | None = None) -> list[WaitlistDTO]:
        stmt = select(Waitlist).where(Waitlist.doctor_id == doctor_id)
        if status is not None:
            stmt = stmt.where(Waitlist.status == status)
        return [_to_waitlist_dto(w) for w in self._session.execute(stmt).scalars()]

    def leave(self, id: uuid.UUID) -> bool:
        row = self._session.get(Waitlist, id)
        if row is None:
            return False
        row.status = "cancelled"
        self._session.flush()
        return True

    def notify_next(self, doctor_id: uuid.UUID) -> WaitlistDTO | None:
        """Mark the earliest still-"waiting" entry for this doctor as
        "notified" and return it — matches the blob's cancellation-triggers-
        waitlist-notify flow in appointments.py::update_appointment_status."""
        stmt = (
            select(Waitlist)
            .where(Waitlist.doctor_id == doctor_id, Waitlist.status == "waiting")
            .order_by(Waitlist.joined_at.asc())
            .limit(1)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        row.status = "notified"
        self._session.flush()
        return _to_waitlist_dto(row)


@dataclass(frozen=True, slots=True)
class BlockedDateDTO:
    id: uuid.UUID
    doctor_id: uuid.UUID
    date: str
    created_at: datetime
    reason: str | None = None


def _to_blocked_date_dto(b: BlockedDate) -> BlockedDateDTO:
    return BlockedDateDTO(id=b.id, doctor_id=b.doctor_id, date=b.date, created_at=b.created_at, reason=b.reason)


class BlockedDateRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def block(self, doctor_id: uuid.UUID, date: str, *, reason: str | None = None) -> BlockedDateDTO:
        row = BlockedDate(id=uuid.uuid4(), doctor_id=doctor_id, date=date, reason=reason)
        self._session.add(row)
        self._session.flush()
        return _to_blocked_date_dto(row)

    def list_for_doctor(self, doctor_id: uuid.UUID) -> list[BlockedDateDTO]:
        stmt = select(BlockedDate).where(BlockedDate.doctor_id == doctor_id)
        return [_to_blocked_date_dto(b) for b in self._session.execute(stmt).scalars()]

    def unblock(self, doctor_id: uuid.UUID, date: str) -> bool:
        stmt = select(BlockedDate).where(BlockedDate.doctor_id == doctor_id, BlockedDate.date == date)
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def is_blocked(self, doctor_id: uuid.UUID, date: str) -> bool:
        stmt = select(BlockedDate).where(BlockedDate.doctor_id == doctor_id, BlockedDate.date == date)
        return self._session.execute(stmt).scalar_one_or_none() is not None


@dataclass(frozen=True, slots=True)
class RescheduleProposalDTO:
    id: uuid.UUID
    appointment_id: uuid.UUID
    proposed_slot: datetime
    status: str
    proposed_at: datetime
    reason: str | None = None
    proposed_by: str | None = None
    responded_at: datetime | None = None


def _to_reschedule_proposal_dto(p: RescheduleProposal) -> RescheduleProposalDTO:
    return RescheduleProposalDTO(
        id=p.id, appointment_id=p.appointment_id, proposed_slot=p.proposed_slot,
        status=p.status, proposed_at=p.proposed_at, reason=p.reason,
        proposed_by=p.proposed_by, responded_at=p.responded_at,
    )


class RescheduleProposalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self, appointment_id: uuid.UUID, *, proposed_slot: datetime,
        reason: str | None = None, proposed_by: str | None = None,
    ) -> RescheduleProposalDTO:
        """appointment_id is unique (1:1) and the blob route always overwrites
        the whole proposal dict — a real upsert, not create-then-update.
        Resets status to "pending" and clears responded_at, same as a fresh
        blob dict would."""
        stmt = select(RescheduleProposal).where(RescheduleProposal.appointment_id == appointment_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            row = RescheduleProposal(id=uuid.uuid4(), appointment_id=appointment_id, proposed_slot=proposed_slot)
            self._session.add(row)
        row.proposed_slot = proposed_slot
        row.reason = reason
        row.proposed_by = proposed_by
        row.status = "pending"
        row.responded_at = None
        self._session.flush()
        return _to_reschedule_proposal_dto(row)

    def set_status(self, appointment_id: uuid.UUID, status: str, *, responded_at: datetime) -> RescheduleProposalDTO | None:
        stmt = select(RescheduleProposal).where(RescheduleProposal.appointment_id == appointment_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        row.status = status
        row.responded_at = responded_at
        self._session.flush()
        return _to_reschedule_proposal_dto(row)

    def get_for_appointment(self, appointment_id: uuid.UUID) -> RescheduleProposalDTO | None:
        stmt = select(RescheduleProposal).where(RescheduleProposal.appointment_id == appointment_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return _to_reschedule_proposal_dto(row) if row else None
