from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from diffdx.db.models.files import UploadedFile


@dataclass(frozen=True, slots=True)
class UploadedFileDTO:
    id: uuid.UUID
    filename: str
    storage_path: str
    appointment_id: uuid.UUID | None
    session_id: str | None
    suggested_test_id: uuid.UUID | None
    content_type: str | None
    uploaded_at: datetime


def _to_dto(f: UploadedFile) -> UploadedFileDTO:
    return UploadedFileDTO(
        id=f.id,
        filename=f.filename,
        storage_path=f.storage_path,
        appointment_id=f.appointment_id,
        session_id=f.session_id,
        suggested_test_id=f.suggested_test_id,
        content_type=f.content_type,
        uploaded_at=f.uploaded_at,
    )


class FileRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        filename: str,
        storage_path: str,
        id: uuid.UUID | None = None,
        appointment_id: uuid.UUID | None = None,
        session_id: str | None = None,
        suggested_test_id: uuid.UUID | None = None,
        content_type: str | None = None,
        uploaded_at: datetime | None = None,
    ) -> UploadedFileDTO:
        f = UploadedFile(
            id=id or uuid.uuid4(),
            filename=filename,
            storage_path=storage_path,
            appointment_id=appointment_id,
            session_id=session_id,
            suggested_test_id=suggested_test_id,
            content_type=content_type,
        )
        if uploaded_at is not None:
            f.uploaded_at = uploaded_at
        self._session.add(f)
        self._session.flush()
        return _to_dto(f)

    def list_for_appointment(self, appointment_id: uuid.UUID) -> list[UploadedFileDTO]:
        stmt = select(UploadedFile).where(UploadedFile.appointment_id == appointment_id)
        return [_to_dto(f) for f in self._session.execute(stmt).scalars()]
