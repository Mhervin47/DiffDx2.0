"""Shared DSR inventory/erasure logic — used by two callers:

  - admin_portal/routers/dsr.py — an admin erasing a subject after
    reviewing their inventory (search -> inventory -> erase).
  - diffdx/routers/account_deletion.py — a patient erasing their own
    account immediately, no admin involved.

Extracted from admin_portal/routers/dsr.py (which owned this logic first
and documents the full reasoning behind it) so neither caller re-derives
or duplicates it. Read that file's module docstring for the complete
discussion; the three load-bearing findings it documents apply here
unchanged, since this *is* that same code, just relocated:

  1. Identity lives relationally (users/patients tables via
     UserRepository), not in the blob store — diffdx.legacy_store's
     store['users'] is never read or written anywhere in this codebase.
  2. True single-transaction atomicity across the blob store and the
     relational DB is not achievable with the existing infrastructure —
     the blob store (diffdx.legacy_store's `store` table) is written
     through its own raw psycopg2/sqlite3 connections, entirely separate
     from the SQLAlchemy engine/session used everywhere else. So
     `_erase_relational` (one real SQLAlchemy transaction, committed
     first — it carries the FK constraints and the compliance-critical
     pseudonymisation) and `_erase_blob_and_disk` (a best-effort
     follow-up, failures collected in `warnings`, never silently
     swallowed) are two separate steps, not one atomic operation.
  3. DiagnosticSession.patient_id is
     ForeignKey("patients.user_id", ondelete="SET NULL") — NOT CASCADE.
     Deleting the Patient row alone would silently ORPHAN
     diagnostic_sessions/session_turns (patient_id -> NULL) rather than
     erase them. DiagnosticSession rows for this patient are deleted
     explicitly, before the user row itself, rather than relying on the
     FK cascade to do it.

This module owns the mechanics; each caller owns its own authorization
(who may erase whom), confirmation checks, feature flag, audit action
name, and response shape around these calls.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update

from diffdx.db.models.audit import AuditLogEntry
from diffdx.db.models.clinical import DiagnosticSession, SessionTurn
from diffdx.db.models.files import UploadedFile
from diffdx.db.models.messaging import Message, MessageThread
from diffdx.db.models.scheduling import Appointment
from diffdx.db.models.user import User
from diffdx.legacy_store import _db_save, _load_appointments, _save_appointments
from diffdx.repositories.users import UserRepository

# dsr_erasure.py -> diffdx -> src -> loop1 (3 parent hops — one fewer than
# admin_portal/routers/dsr.py's own copy of this constant, which sits one
# directory deeper).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_LOGS_SESSIONS_DIR = _REPO_ROOT / "logs" / "sessions"
_LOGS_FINAL_RECORDS_DIR = _REPO_ROOT / "logs" / "final_records"

# Fixed, arbitrary namespace UUID for deriving stable pseudonym ids —
# uuid.uuid5(NAMESPACE, str(user_id)) is deterministic, so the same subject
# always pseudonymises to the same id even if erase is somehow invoked twice.
_PSEUDONYM_NAMESPACE = uuid.UUID("6e6f7420-6120-7265-616c-207573657200")


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def _session_ids_for_patient(db, user_id: uuid.UUID) -> list[str]:
    rows = db.execute(select(DiagnosticSession.id).where(DiagnosticSession.patient_id == user_id)).all()
    return [str(r[0]) for r in rows]


def _count_disk_session_artifacts(session_ids: list[str]) -> int:
    count = 0
    for sid in session_ids:
        if (_LOGS_SESSIONS_DIR / f"session_{sid}.jsonl").exists():
            count += 1
        if (_LOGS_FINAL_RECORDS_DIR / f"final_{sid}.json").exists():
            count += 1
    return count


def _count_session_reports(session_ids: list[str]) -> int:
    from diffdx.legacy_store import _load_session_report_from_db

    return sum(1 for sid in session_ids if _load_session_report_from_db(sid) is not None)


def _build_inventory(db, user: User) -> list[dict[str, Any]]:
    uid = user.id
    uid_str = str(uid)

    session_ids = _session_ids_for_patient(db, uid)
    n_sessions = len(session_ids)
    n_turns = (
        db.execute(select(func.count()).select_from(SessionTurn).where(SessionTurn.session_id.in_(
            [uuid.UUID(s) for s in session_ids]
        ))).scalar_one()
        if session_ids else 0
    )
    n_reports = _count_session_reports(session_ids)
    n_disk_artifacts = _count_disk_session_artifacts(session_ids)

    n_appointments = db.execute(
        select(func.count()).select_from(Appointment).where(Appointment.patient_id == uid)
    ).scalar_one()

    n_threads = db.execute(
        select(func.count()).select_from(MessageThread).where(MessageThread.patient_id == uid)
    ).scalar_one()
    n_messages = db.execute(
        select(func.count()).select_from(Message).join(MessageThread, Message.thread_id == MessageThread.id)
        .where(MessageThread.patient_id == uid)
    ).scalar_one()

    n_files = db.execute(
        select(func.count()).select_from(UploadedFile)
        .join(Appointment, UploadedFile.appointment_id == Appointment.id)
        .where(Appointment.patient_id == uid)
    ).scalar_one()

    n_audit = db.execute(
        select(func.count()).select_from(AuditLogEntry).where(AuditLogEntry.actor_user_id == uid)
    ).scalar_one()

    return [
        {"label": "Identity", "tables": ["users", "patients"], "record_count": 1, "detail": None, "retained": False},
        {
            "label": "Consultations", "tables": ["diagnostic_sessions", "session_turns"],
            "record_count": n_sessions + n_turns, "detail": f"{n_sessions} sessions, {n_turns} turns",
            "retained": False,
        },
        {
            "label": "Session reports (critiques + transcript)", "tables": ["store['session_report:{id}']"],
            "record_count": n_reports, "detail": "blob store only — no relational equivalent (see module docstring)",
            "retained": False,
        },
        {
            "label": "Appointments", "tables": ["appointments", "store['appointments']"],
            "record_count": n_appointments, "detail": "dual-written — both cleared", "retained": False,
        },
        {
            "label": "Messages", "tables": ["message_threads", "messages", "store['messages']"],
            "record_count": n_threads + n_messages, "detail": f"{n_threads} thread(s), {n_messages} message(s)",
            "retained": False,
        },
        {
            "label": "Files", "tables": ["uploaded_files", "web/data/files/"],
            "record_count": n_files, "detail": None, "retained": False,
        },
        {
            "label": "Session logs on disk", "tables": ["logs/sessions/*.jsonl", "logs/final_records/*.json"],
            "record_count": n_disk_artifacts, "detail": "full transcript, PHI, outside the database",
            "retained": False,
        },
        {
            "label": "Audit trail", "tables": ["audit_log_entries"],
            "record_count": n_audit, "detail": "pseudonymised on erase, never deleted", "retained": True,
        },
    ]


# ---------------------------------------------------------------------------
# Erase
# ---------------------------------------------------------------------------

def _pseudo_id(user_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_PSEUDONYM_NAMESPACE, str(user_id))


def _erase_relational(db, user: User) -> tuple[dict[str, int], list[str], uuid.UUID]:
    """The one real SQLAlchemy transaction: pseudonymise the audit trail,
    explicitly delete DiagnosticSession (see module docstring re: the
    SET NULL trap), then delete the user row — DB-level ON DELETE CASCADE
    handles Patient -> Appointments/MessageThreads/Waitlist/Dependents and
    Appointments -> its sub-entities/UploadedFile from there."""
    uid = user.id
    pseudo_id = _pseudo_id(uid)

    UserRepository(db).shadow_user(
        id=pseudo_id, name="", email=f"erased+{pseudo_id}@invalid", password_hash="", role=user.role,
    )
    db.execute(update(AuditLogEntry).where(AuditLogEntry.actor_user_id == uid).values(actor_user_id=pseudo_id))

    session_uuids = [row[0] for row in db.execute(
        select(DiagnosticSession.id).where(DiagnosticSession.patient_id == uid)
    ).all()]
    n_turns = (
        db.execute(select(func.count()).select_from(SessionTurn).where(SessionTurn.session_id.in_(session_uuids)))
        .scalar_one() if session_uuids else 0
    )
    n_sessions = len(session_uuids)
    if session_uuids:
        db.execute(delete(DiagnosticSession).where(DiagnosticSession.patient_id == uid))

    n_appointments = db.execute(
        select(func.count()).select_from(Appointment).where(Appointment.patient_id == uid)
    ).scalar_one()
    n_threads = db.execute(
        select(func.count()).select_from(MessageThread).where(MessageThread.patient_id == uid)
    ).scalar_one()
    n_files = db.execute(
        select(func.count()).select_from(UploadedFile)
        .join(Appointment, UploadedFile.appointment_id == Appointment.id)
        .where(Appointment.patient_id == uid)
    ).scalar_one()

    db.delete(user)  # cascades: Patient, and via DB FK CASCADE: appointments/threads/waitlist/dependents/files
    db.commit()

    return {
        "users": 1, "diagnostic_sessions": n_sessions, "session_turns": n_turns,
        "appointments": n_appointments, "message_threads": n_threads, "uploaded_files": n_files,
    }, [str(s) for s in session_uuids], pseudo_id


def _erase_blob_and_disk(user_id_str: str, session_ids: list[str]) -> tuple[dict[str, int], list[str]]:
    """Best-effort follow-up, NOT part of the SQLAlchemy transaction above —
    the blob store lives on a separate connection (see module docstring).
    Failures are collected as warnings, never silently swallowed."""
    counts = {"blob_appointments": 0, "blob_messages": 0, "session_reports": 0, "disk_artifacts": 0}
    warnings: list[str] = []

    try:
        appts = _load_appointments()
        remaining = {k: v for k, v in appts.items() if v.get("patient_user_id") != user_id_str}
        counts["blob_appointments"] = len(appts) - len(remaining)
        if counts["blob_appointments"]:
            _save_appointments(remaining)
    except Exception as exc:
        warnings.append(f"Could not clear blob appointments: {exc}")

    try:
        from diffdx.routers.messaging import _load_messages, _save_messages

        msgs = _load_messages()
        remaining = [m for m in msgs if m.get("patient_user_id") != user_id_str]
        counts["blob_messages"] = len(msgs) - len(remaining)
        if counts["blob_messages"]:
            _save_messages(remaining)
    except Exception as exc:
        warnings.append(f"Could not clear blob messages: {exc}")

    for sid in session_ids:
        try:
            from diffdx.legacy_store import _load_session_report_from_db

            if _load_session_report_from_db(sid) is not None:
                _db_save(f"session_report:{sid}", None)
                counts["session_reports"] += 1
        except Exception as exc:
            warnings.append(f"Could not clear session report {sid}: {exc}")

    for sid in session_ids:
        for path in (_LOGS_SESSIONS_DIR / f"session_{sid}.jsonl", _LOGS_FINAL_RECORDS_DIR / f"final_{sid}.json"):
            try:
                if path.exists():
                    path.unlink()
                    counts["disk_artifacts"] += 1
            except Exception as exc:
                warnings.append(f"Could not delete {path}: {exc}")

    return counts, warnings
