"""
Data Subject Request console — admin portal, Phase 2, Task 8.

Built last, deliberately, per the build brief — this is the most sensitive
surface in the portal.

--- A real discrepancy from the Phase 2 brief's Section 1.10, found while
mapping where PHI actually lives (reported here, not silently adopted) ---
The brief claims "the authoritative user records live in a blob store, not
the users table" and that DSR search must search the blob store. Against
the CURRENT source, that is backwards for identity specifically:
diffdx.legacy_store._load_users() no longer reads any blob at all — it
composes its (blob-*shaped*, for backward compatibility) dict from
UserRepository.list_all(), i.e. the relational users/patients tables. A
repo-wide grep confirms store['users'] is never read or written anywhere.
This is the post-identity-cutover state (see TASK6_IDENTITY_CUTOVER.md).
So: identity search here queries the relational tables directly, NOT a
blob collection. The brief IS still correct that appointments and messages
remain dual-written (store['appointments'] / store['messages'] are both
still actively read and written by existing routers), and that session
reports/critiques live ONLY in store['session_report:{id}'] with no
relational equivalent (1.9) — both confirmed by reading the real source,
and both handled below.

--- A second, structural limitation, also reported rather than glossed
over: true single-transaction atomicity across the blob store AND the
relational DB is not achievable with the existing infrastructure. The blob
store (diffdx.legacy_store's `store` table) is written through its own
raw psycopg2/sqlite3 connections, entirely separate from the SQLAlchemy
engine/session used everywhere else. So "one transaction" here means: the
relational deletes and the audit pseudonymisation are one real SQLAlchemy
transaction (atomic with respect to each other, and committed first,
since that side carries the FK constraints and the compliance-critical
pseudonymisation). The blob-store and on-disk cleanup happen as a
best-effort follow-up and their outcome is reported in the receipt's
`warnings` field — never silently swallowed — rather than claiming a
cross-store atomicity guarantee the codebase cannot actually provide.

--- A third finding worth flagging: DiagnosticSession.patient_id is
ForeignKey("patients.user_id", ondelete="SET NULL") — NOT CASCADE. Deleting
the Patient row alone would silently ORPHAN diagnostic_sessions/session_turns
(patient_id -> NULL) rather than erase them, which is exactly the kind of
"looks erased but isn't" failure this whole console exists to prevent.
DiagnosticSession rows for this patient are deleted explicitly, before the
user row itself, rather than relying on the FK cascade to do it.

No `turn_critiques` row in the inventory — that table does not exist (1.9).
Critiques live inside store['session_report:{id}'], covered under
"Session reports" below.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select, update

from diffdx.audit import log_audit_event
from diffdx.db.engine import get_sessionmaker
from diffdx.db.models.audit import AuditLogEntry
from diffdx.db.models.clinical import DiagnosticSession, SessionTurn
from diffdx.db.models.files import UploadedFile
from diffdx.db.models.messaging import Message, MessageThread
from diffdx.db.models.scheduling import Appointment, Waitlist
from diffdx.db.models.user import Dependent, Patient, User
from diffdx.dependencies import require_role
from diffdx.legacy_store import _db_save, _load_appointments, _save_appointments
from diffdx.repositories.users import UserRepository

router = APIRouter(tags=["admin_portal"])
_log = logging.getLogger(__name__)

# routers/dsr.py -> routers -> admin_portal -> src -> loop1 (4 parent hops)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_LOGS_SESSIONS_DIR = _REPO_ROOT / "logs" / "sessions"
_LOGS_FINAL_RECORDS_DIR = _REPO_ROOT / "logs" / "final_records"

_SEARCH_LIMIT = 50
_ENABLE_ERASE_ENV = "ADMIN_PORTAL_ENABLE_DSR_ERASE"
# Fixed, arbitrary namespace UUID for deriving stable pseudonym ids —
# uuid.uuid5(NAMESPACE, str(user_id)) is deterministic, so the same subject
# always pseudonymises to the same id even if erase is somehow invoked twice.
_PSEUDONYM_NAMESPACE = uuid.UUID("6e6f7420-6120-7265-616c-207573657200")


class EraseRequest(BaseModel):
    confirm_email: str


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@router.get("/api/admin/subjects")
def search_subjects(
    request: Request,
    q: str | None = Query(default=None),
    _admin: dict = Depends(require_role("admin")),
) -> dict[str, Any]:
    with get_sessionmaker()() as db:
        stmt = select(User).join(Patient, Patient.user_id == User.id).where(User.role == "patient")
        if q:
            needle = f"%{q.lower().strip()}%"
            stmt = stmt.where(or_(func.lower(User.name).like(needle), func.lower(User.email).like(needle)))
        stmt = stmt.order_by(User.created_at.desc()).limit(_SEARCH_LIMIT)
        users = db.execute(stmt).scalars().all()

        items = []
        for u in users:
            session_count = db.execute(
                select(func.count()).select_from(DiagnosticSession).where(DiagnosticSession.patient_id == u.id)
            ).scalar_one()
            items.append({
                "user_id": str(u.id),
                "name": u.name,
                "email": u.email,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "session_count": session_count,
            })

    # resource_id is the result count, NOT the query string `q` itself —
    # a search term is typically a patient's name or email fragment, and
    # audit_log_entries must never become a second place PHI lives. "An
    # admin searched, when, and how many matches" is enough accountability
    # signal without storing what was searched for.
    _audit(request, _admin, "GET /api/admin/subjects", "user", f"{len(items)}_results")
    return {"status": "ok", "items": items}


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


@router.get("/api/admin/subjects/{user_id}/inventory")
def get_inventory(user_id: str, request: Request, _admin: dict = Depends(require_role("admin"))) -> dict[str, Any]:
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Subject not found.")

    with get_sessionmaker()() as db:
        user = db.get(User, uid)
        if user is None or user.role != "patient":
            raise HTTPException(status_code=404, detail="Subject not found.")
        categories = _build_inventory(db, user)
        result = {
            "status": "ok",
            "user_id": str(user.id),
            "name": user.name,
            "email": user.email,
            "categories": categories,
        }

    _audit(request, _admin, "GET /api/admin/subjects/{id}/inventory", "user", user_id)
    return result


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@router.get("/api/admin/subjects/{user_id}/export")
def export_subject(user_id: str, request: Request, _admin: dict = Depends(require_role("admin"))) -> dict[str, Any]:
    from diffdx.legacy_store import _load_session_report_from_db

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Subject not found.")

    with get_sessionmaker()() as db:
        user = db.get(User, uid)
        if user is None or user.role != "patient":
            raise HTTPException(status_code=404, detail="Subject not found.")

        session_ids = _session_ids_for_patient(db, uid)
        sessions = []
        for sid in session_ids:
            sess = db.get(DiagnosticSession, uuid.UUID(sid))
            turns = db.execute(
                select(SessionTurn).where(SessionTurn.session_id == uuid.UUID(sid)).order_by(SessionTurn.turn_index)
            ).scalars().all()
            sessions.append({
                "session_id": sid,
                "chief_complaint": sess.chief_complaint if sess else None,
                "primary_diagnosis": sess.primary_diagnosis if sess else None,
                "started_at": sess.started_at.isoformat() if sess and sess.started_at else None,
                "ended_at": sess.ended_at.isoformat() if sess and sess.ended_at else None,
                "turns": [
                    {
                        "turn_index": t.turn_index, "question": t.question, "patient_answer": t.patient_answer,
                        "confidence": t.confidence, "differential": t.differential,
                    }
                    for t in turns
                ],
                "report": _load_session_report_from_db(sid),
            })

        appointments = [a for a in _load_appointments().values() if a.get("patient_user_id") == user_id]

        from diffdx.routers.messaging import _load_messages

        patient_messages = [m for m in _load_messages() if m.get("patient_user_id") == user_id]

        n_files = db.execute(
            select(func.count()).select_from(UploadedFile)
            .join(Appointment, UploadedFile.appointment_id == Appointment.id)
            .where(Appointment.patient_id == uid)
        ).scalar_one()

        bundle = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "subject": {"user_id": str(user.id), "name": user.name, "email": user.email},
            "sessions": sessions,
            "appointments": appointments,
            "messages": patient_messages,
            "files_count": n_files,
            "note": (
                "The audit trail (who accessed this data, including this export) is not included here "
                "— it is retained for compliance and pseudonymised rather than exported or deleted; "
                "see the DSR inventory's 'Audit trail' row."
            ),
        }

    _audit(request, _admin, "GET /api/admin/subjects/{id}/export", "user", user_id)
    return bundle


# ---------------------------------------------------------------------------
# Erase
# ---------------------------------------------------------------------------

def _pseudo_id(user_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_PSEUDONYM_NAMESPACE, str(user_id))


def _erase_relational(db, user: User) -> dict[str, int]:
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


@router.delete("/api/admin/subjects/{user_id}")
def erase_subject(
    user_id: str,
    body: EraseRequest,
    request: Request,
    dry_run: bool = Query(default=False),
    _admin: dict = Depends(require_role("admin")),
) -> dict[str, Any]:
    if not dry_run and os.environ.get(_ENABLE_ERASE_ENV, "").lower() not in ("1", "true", "yes"):
        raise HTTPException(
            status_code=501,
            detail=(
                f"DSR erase is disabled by default. Set {_ENABLE_ERASE_ENV}=true to enable it, "
                "or pass ?dry_run=true to preview what would be deleted without deleting anything."
            ),
        )

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Subject not found.")

    with get_sessionmaker()() as db:
        user = db.get(User, uid)
        if user is None or user.role != "patient":
            raise HTTPException(status_code=404, detail="Subject not found.")
        if user.email.lower().strip() != body.confirm_email.lower().strip():
            raise HTTPException(status_code=400, detail="confirm_email does not match this subject.")

        if dry_run:
            categories = _build_inventory(db, user)
            would_erase = {c["label"]: c["record_count"] for c in categories if not c["retained"]}
            _audit(request, _admin, "subject_erasure_dry_run", "user", user_id)
            return {
                "status": "ok", "dry_run": True, "user_id": user_id,
                "would_erase": would_erase,
                "retained": {"audit_log": next(c["record_count"] for c in categories if c["label"] == "Audit trail")},
                "erased_at": None, "audit_entry_id": None, "warnings": [],
            }

        try:
            erased_relational, session_ids, pseudo_id = _erase_relational(db, user)
        except Exception:
            db.rollback()
            _log.error("DSR erase failed for %s — relational transaction rolled back", user_id, exc_info=True)
            raise HTTPException(status_code=500, detail="Erase failed — no changes were made.")

    blob_disk_counts, warnings = _erase_blob_and_disk(user_id, session_ids)

    erased_at = datetime.now(timezone.utc).isoformat()
    # The erasure itself writes an AuditLogEntry — actor is the admin who
    # performed it (not the now-pseudonymised subject).
    log_audit_event(
        actor=_admin, action="subject_erasure", resource_type="user", resource_id=user_id,
        ip_address=request.client.host if request.client else None,
    )
    # Fetch the id of the entry we just wrote so the receipt can link to it.
    with get_sessionmaker()() as db2:
        latest = db2.execute(
            select(AuditLogEntry.id)
            .where(AuditLogEntry.action == "subject_erasure", AuditLogEntry.resource_id == user_id)
            .order_by(AuditLogEntry.created_at.desc()).limit(1)
        ).scalar_one_or_none()

    return {
        "status": "ok",
        "dry_run": False,
        "erased": {**erased_relational, **blob_disk_counts},
        "retained": {"audit_log": "pseudonymised, not deleted"},
        "pseudonym_id": str(pseudo_id),
        "audit_entry_id": str(latest) if latest else None,
        "erased_at": erased_at,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Audit logging for the three read/export routes (the erase route logs
# itself above, with action="subject_erasure") — AuditLogMiddleware does not
# cover /api/admin/* (build brief 1.11), so every DSR route must call
# log_audit_event() itself. An admin reading or exporting PHI with no audit
# record is exactly the failure this console exists to demonstrate against.
# ---------------------------------------------------------------------------

def _audit(request: Request | None, admin: dict, action: str, resource_type: str, resource_id: str | None) -> None:
    log_audit_event(
        actor=admin, action=action, resource_type=resource_type, resource_id=resource_id,
        ip_address=request.client.host if request and request.client else None,
    )
