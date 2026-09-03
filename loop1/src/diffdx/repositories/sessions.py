from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from diffdx.db.models.clinical import DiagnosticSession, SessionTurn


@dataclass(frozen=True, slots=True)
class DiagnosticSessionDTO:
    id: uuid.UUID
    patient_id: uuid.UUID | None
    chief_complaint: str | None
    primary_diagnosis: str | None
    termination_reason: str | None
    started_at: datetime | None
    ended_at: datetime | None
    total_turns: int | None
    final_differential: list | None
    closing_turn: dict | None


@dataclass(frozen=True, slots=True)
class SessionTurnDTO:
    id: uuid.UUID
    session_id: uuid.UUID
    turn_index: int
    question: str | None
    rationale: str | None
    biggest_uncertainty: str | None
    patient_answer: str | None
    confidence: float | None
    differential: list | None
    doctor_output: dict | None


def _to_session_dto(s: DiagnosticSession) -> DiagnosticSessionDTO:
    return DiagnosticSessionDTO(
        id=s.id,
        patient_id=s.patient_id,
        chief_complaint=s.chief_complaint,
        primary_diagnosis=s.primary_diagnosis,
        termination_reason=s.termination_reason,
        started_at=s.started_at,
        ended_at=s.ended_at,
        total_turns=s.total_turns,
        final_differential=s.final_differential,
        closing_turn=s.closing_turn,
    )


def _to_turn_dto(t: SessionTurn) -> SessionTurnDTO:
    return SessionTurnDTO(
        id=t.id,
        session_id=t.session_id,
        turn_index=t.turn_index,
        question=t.question,
        rationale=t.rationale,
        biggest_uncertainty=t.biggest_uncertainty,
        patient_answer=t.patient_answer,
        confidence=t.confidence,
        differential=t.differential,
        doctor_output=t.doctor_output,
    )


class DiagnosticSessionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, session_id: uuid.UUID) -> DiagnosticSessionDTO | None:
        rec = self._session.get(DiagnosticSession, session_id)
        return _to_session_dto(rec) if rec else None

    def upsert(
        self,
        session_id: uuid.UUID,
        *,
        patient_id: uuid.UUID | None = None,
        chief_complaint: str | None = None,
        primary_diagnosis: str | None = None,
        termination_reason: str | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        total_turns: int | None = None,
        final_differential: list | None = None,
        closing_turn: dict | None = None,
    ) -> DiagnosticSessionDTO:
        rec = self._session.get(DiagnosticSession, session_id)
        if rec is None:
            rec = DiagnosticSession(id=session_id)
            self._session.add(rec)
        for field, value in (
            ("patient_id", patient_id),
            ("chief_complaint", chief_complaint),
            ("primary_diagnosis", primary_diagnosis),
            ("termination_reason", termination_reason),
            ("started_at", started_at),
            ("ended_at", ended_at),
            ("total_turns", total_turns),
            ("final_differential", final_differential),
            ("closing_turn", closing_turn),
        ):
            if value is not None:
                setattr(rec, field, value)
        self._session.flush()
        return _to_session_dto(rec)

    def add_turn(
        self,
        session_id: uuid.UUID,
        turn_index: int,
        *,
        id: uuid.UUID | None = None,
        question: str | None = None,
        rationale: str | None = None,
        biggest_uncertainty: str | None = None,
        patient_answer: str | None = None,
        confidence: float | None = None,
        differential: list | None = None,
        doctor_output: dict | None = None,
    ) -> SessionTurnDTO:
        turn = SessionTurn(
            id=id or uuid.uuid4(),
            session_id=session_id,
            turn_index=turn_index,
            question=question,
            rationale=rationale,
            biggest_uncertainty=biggest_uncertainty,
            patient_answer=patient_answer,
            confidence=confidence,
            differential=differential,
            doctor_output=doctor_output,
        )
        self._session.add(turn)
        self._session.flush()
        return _to_turn_dto(turn)

    def list_turns(self, session_id: uuid.UUID) -> list[SessionTurnDTO]:
        stmt = (
            select(SessionTurn)
            .where(SessionTurn.session_id == session_id)
            .order_by(SessionTurn.turn_index)
        )
        return [_to_turn_dto(t) for t in self._session.execute(stmt).scalars()]
