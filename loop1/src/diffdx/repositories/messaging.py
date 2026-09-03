from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from diffdx.db.models.messaging import Message, MessageThread


@dataclass(frozen=True, slots=True)
class MessageThreadDTO:
    id: uuid.UUID
    patient_id: uuid.UUID
    doctor_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class MessageDTO:
    id: uuid.UUID
    thread_id: uuid.UUID
    appointment_id: uuid.UUID | None
    sender_role: str
    body: str
    sent_at: datetime
    read: bool


def _to_thread_dto(t: MessageThread) -> MessageThreadDTO:
    return MessageThreadDTO(id=t.id, patient_id=t.patient_id, doctor_id=t.doctor_id)


def _to_message_dto(m: Message) -> MessageDTO:
    return MessageDTO(
        id=m.id,
        thread_id=m.thread_id,
        appointment_id=m.appointment_id,
        sender_role=m.sender_role,
        body=m.body,
        sent_at=m.sent_at,
        read=m.read,
    )


class MessagingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create_thread(self, patient_id: uuid.UUID, doctor_id: uuid.UUID) -> MessageThreadDTO:
        stmt = select(MessageThread).where(
            MessageThread.patient_id == patient_id, MessageThread.doctor_id == doctor_id
        )
        thread = self._session.execute(stmt).scalar_one_or_none()
        if thread is None:
            thread = MessageThread(patient_id=patient_id, doctor_id=doctor_id)
            self._session.add(thread)
            self._session.flush()
        return _to_thread_dto(thread)

    def send_message(
        self,
        thread_id: uuid.UUID,
        *,
        sender_role: str,
        body: str,
        id: uuid.UUID | None = None,
        appointment_id: uuid.UUID | None = None,
        sent_at: datetime | None = None,
        read: bool = False,
    ) -> MessageDTO:
        msg = Message(
            id=id or uuid.uuid4(),
            thread_id=thread_id,
            appointment_id=appointment_id,
            sender_role=sender_role,
            body=body,
            read=read,
        )
        if sent_at is not None:
            msg.sent_at = sent_at
        self._session.add(msg)
        self._session.flush()
        return _to_message_dto(msg)

    def list_messages(self, thread_id: uuid.UUID) -> list[MessageDTO]:
        stmt = select(Message).where(Message.thread_id == thread_id).order_by(Message.sent_at)
        return [_to_message_dto(m) for m in self._session.execute(stmt).scalars()]
