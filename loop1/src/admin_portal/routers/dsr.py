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

The inventory/erase mechanics described above (_build_inventory,
_erase_relational, _erase_blob_and_disk, and everything they depend on) now
live in diffdx.dsr_erasure, not here — this file imports them rather than
defining them, so the patient-facing self-service deletion flow
(diffdx/routers/account_deletion.py) can reuse the exact same logic instead
of a second, drift-prone implementation. See that module's docstring for
the full reasoning; every constraint above still applies unchanged, it's
just relocated code, not relocated behavior.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from diffdx.audit import log_audit_event
from diffdx.db.engine import get_sessionmaker
from diffdx.db.models.audit import AuditLogEntry
from diffdx.db.models.clinical import DiagnosticSession, SessionTurn
from diffdx.db.models.dsr import DsrErasureRequest
from diffdx.db.models.files import UploadedFile
from diffdx.db.models.scheduling import Appointment
from diffdx.db.models.user import Patient, User
from diffdx.dependencies import require_role
from diffdx.dsr_erasure import _build_inventory, _erase_blob_and_disk, _erase_relational, _session_ids_for_patient
from diffdx.legacy_store import _load_appointments

router = APIRouter(tags=["admin_portal"])
_log = logging.getLogger(__name__)

_SEARCH_LIMIT = 50
_ENABLE_ERASE_ENV = "ADMIN_PORTAL_ENABLE_DSR_ERASE"


class EraseRequest(BaseModel):
    # Exactly one of these applies, chosen by the subject's own
    # user.deleted_at state at erase time — see erase_subject. A subject
    # who has self-deactivated (diffdx/routers/account_deletion.py) no
    # longer has a real email on file (it was already scrubbed to
    # erased+{uid}@invalid), so confirm_email can't be matched against
    # anything meaningful for them; confirm_user_id exists for exactly
    # that case.
    confirm_email: str | None = None
    confirm_user_id: str | None = None


class DenyRequestBody(BaseModel):
    note: str


# ---------------------------------------------------------------------------
# Request queue — a thin front door to everything below. A patient can ask,
# from their own profile (diffdx.routers.dsr_requests), that their data be
# deleted; that just writes a DsrErasureRequest row with status="pending".
# There is no "approve" endpoint here: approving a request IS an admin
# running the existing search -> inventory -> erase flow below on that
# subject, unchanged. This section's only mutation is denying a request.
# ---------------------------------------------------------------------------

@router.get("/api/admin/dsr-requests")
def list_dsr_requests(
    request: Request,
    status: str = Query(default="pending"),
    _admin: dict = Depends(require_role("admin")),
) -> dict[str, Any]:
    with get_sessionmaker()() as db:
        stmt = (
            select(DsrErasureRequest, User)
            .join(User, User.id == DsrErasureRequest.user_id)
            .where(DsrErasureRequest.status == status)
            .order_by(DsrErasureRequest.requested_at.desc())
            .limit(_SEARCH_LIMIT)
        )
        rows = db.execute(stmt).all()
        items = [
            {
                "id": str(req.id),
                "user_id": str(user.id),
                "name": user.name,
                "email": user.email,
                "reason": req.reason,
                "requested_at": req.requested_at.isoformat() if req.requested_at else None,
            }
            for req, user in rows
        ]

    _audit(request, _admin, "GET /api/admin/dsr-requests", "dsr_erasure_request", f"{len(items)}_results")
    return {"status": "ok", "items": items}


@router.post("/api/admin/dsr-requests/{request_id}/deny")
def deny_dsr_request(
    request_id: str, body: DenyRequestBody, request: Request, _admin: dict = Depends(require_role("admin")),
) -> dict[str, Any]:
    try:
        rid = uuid.UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Request not found.")

    with get_sessionmaker()() as db:
        req = db.get(DsrErasureRequest, rid)
        if req is None:
            raise HTTPException(status_code=404, detail="Request not found.")
        req.status = "denied"
        req.reviewed_at = datetime.now(timezone.utc)
        req.reviewed_by = uuid.UUID(str(_admin["id"]))
        req.admin_note = body.note
        db.commit()

    _audit(request, _admin, "POST /api/admin/dsr-requests/{id}/deny", "dsr_erasure_request", request_id)
    return {"status": "ok"}


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
                # A deactivated subject's name/email above are scrubbed
                # placeholders, not real values — this flag lets the admin
                # UI label the row distinctly rather than risk reading
                # "Deactivated Patient" as an actual patient's name.
                "deactivated": u.deleted_at is not None,
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
            # Lets the UI switch the erase confirmation from confirm_email
            # to confirm_user_id — see erase_subject and EraseRequest.
            "deactivated": user.deleted_at is not None,
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

        # A subject who has already self-deactivated (see
        # diffdx/routers/account_deletion.py) no longer has a real email
        # on file — user.email was already scrubbed to
        # erased+{uid}@invalid — so confirm_email can never legitimately
        # match anything for them. confirm_user_id (the exact id from the
        # request path, retyped) stands in instead: a redundant-on-purpose
        # confirmation that the admin means to act on this specific id,
        # sourced from wherever they found it (the Audit page, or a
        # search-results row flagged "deactivated": true — see
        # search_subjects) rather than from anything the (former) patient
        # could still supply. A subject who hasn't deactivated keeps the
        # exact original confirm_email check, unchanged.
        if user.deleted_at is not None:
            try:
                confirm_uid_matches = body.confirm_user_id is not None and uuid.UUID(body.confirm_user_id.strip()) == uid
            except ValueError:
                confirm_uid_matches = False
            if not confirm_uid_matches:
                raise HTTPException(status_code=400, detail="confirm_user_id does not match this subject.")
        else:
            if not body.confirm_email or user.email.lower().strip() != body.confirm_email.lower().strip():
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
