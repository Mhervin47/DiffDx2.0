from __future__ import annotations


class RepositoryError(Exception):
    """Base for domain-level errors raised by the repository layer.

    Repositories never swallow exceptions the way the old _db_load did.
    Row-not-found returns None/[]; anything else propagates — either as one
    of these typed errors (mapped to a specific HTTP status by
    diffdx.api_exceptions) or as the raw SQLAlchemy exception (which the
    OperationalError handler turns into a 503).
    """


class ConflictError(RepositoryError):
    """A write violated a uniqueness/integrity constraint — maps to 409.

    The canonical case: booking the same (doctor_id, slot_datetime) twice.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail or message


class NotFoundError(RepositoryError):
    """An operation targeted a row that doesn't exist — maps to 404.

    Repository methods that mutate a specific row by id (not create/list)
    raise this instead of a bare ValueError, so api_exceptions.py can map
    it to a real HTTP status instead of it surfacing as an unhandled 500.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail or message
