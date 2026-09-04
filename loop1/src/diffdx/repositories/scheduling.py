from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from diffdx.db.models.scheduling import BlockedDate, Waitlist


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

    def join(self, patient_id: uuid.UUID, doctor_id: uuid.UUID, *, note: str | None = None) -> WaitlistDTO:
        row = Waitlist(id=uuid.uuid4(), patient_id=patient_id, doctor_id=doctor_id, note=note)
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
