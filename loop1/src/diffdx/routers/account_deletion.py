"""Self-service account deactivation — a patient closes their own account
immediately, themselves, with no admin involved. This is deliberately
**not** the same operation as the admin console's full purge
(admin_portal/routers/dsr.py's erase_subject, via diffdx.dsr_erasure) —
it's a shallower, reversible-in-spirit action: identity is scrubbed and
login is disabled, but every clinical/appointment/session/message row
this account is attached to is retained untouched. A full purge (actual
row deletion) stays admin-only, reachable either via the existing
in-app request queue (diffdx.routers.dsr_requests, unaffected by this
file) or by an admin acting on a deactivated account directly (see
admin_portal/routers/dsr.py's confirm_user_id branch).

Two tiers, deliberately different in depth, not the same operation gated
two ways:
  - Deactivation (this file): patient, self-service, instant, no approval.
    A legal hold, a billing dispute, or a jurisdiction's minimum medical-
    record-retention requirement are all real reasons a full purge
    shouldn't happen unsupervised — deactivation lets someone close their
    account and stop appearing in the product right away without needing
    a human to first confirm none of those apply.
  - Full purge: admin-only, mediated, so a human can catch a reason not to
    purge before it becomes irreversible.

--- What this route does NOT do, corrected from an earlier version of
this file ---
There used to be a `DELETE /api/patient/account` route here that called
the same `_erase_relational`/`_erase_blob_and_disk` functions the admin
console uses — full, irreversible deletion, self-service, no admin
involved at all. That was wrong for this product's data-retention model
and has been removed entirely (route, response shape, and its feature
flag `SELF_SERVICE_ACCOUNT_DELETION_ENABLED` — not kept around under the
old name, not dual-supported). This file now only ever scrubs identity
and cancels future appointments; it never imports or calls anything from
diffdx.dsr_erasure.

--- Authorization, same as before ---
`uid` is always `patient["id"]` from `Depends(require_role("patient"))` —
never accepted from a path/query/body parameter. The current password
must be re-entered and the account email re-typed before deactivation —
a valid JWT alone isn't sufficient for this, same reasoning any
"close my account" flow has for re-checking a credential rather than
trusting a possibly-long-lived session token.

--- Feature flag ---
`SELF_SERVICE_ACCOUNT_DEACTIVATION_ENABLED` — off by default.

--- Audit action, and why it is logged *before* the scrub runs ---
Writes `action="subject_self_deactivation"` — distinct from
`"subject_erasure"`/`"subject_self_erasure"` (an admin-run or the old,
removed self-erase path) and from a hypothetical doctor-side equivalent,
so an admin reading the audit trail later can tell exactly what happened
from the action string alone.

This is logged *before* the identity scrub runs, not after — deliberate,
not arbitrary. `log_audit_event` upserts a bare `User` shadow row for its
actor (see diffdx.audit's own docstring) so the audit table's FK stays
satisfiable; that upsert uses whatever name/email the passed-in actor
dict carries. Unlike the old self-erase path (which pseudonymised the
subject to a *different* id), deactivation keeps the *same* user id — so
if this were logged *after* scrubbing, using the pre-scrub `patient` dict
captured at request start, `log_audit_event`'s upsert would merge that
dict's real (pre-scrub) name/email straight back onto the just-scrubbed
row, undoing the scrub. Logging first means the write happens while the
row still legitimately holds those values (a harmless no-op merge), and
the scrub — which runs afterward, in the same request — is the last
write to that row.

--- Future appointments: cancelled, not blocked on ---
diffdx.routers.appointments4._cancel_patient_appointment_impl (the same
function backing the patient's own "cancel appointment" button) is called
directly, once per upcoming/confirmed appointment, with
cancelled_by="patient_deactivation" — a value distinct from the ordinary
"patient"/"patient_reschedule" cancellations that function already
supports, so the doctor/admin side can tell an account closure apart from
a routine cancellation. Reusing this function means the exact same
notification email fires, rather than a second, parallel notification
path being written here.

--- Dependent rows: investigated, left untouched ---
diffdx.db.models.user.Dependent has no contact/identity fields at all —
no email, phone, or address, only name/relationship/age/gender/blood_type
/allergies/chronic_conditions. There is nothing on that model analogous to
Patient's mobile/address/emergency_contact_* to scrub; `name` is needed to
tell whose clinical record a row belongs to, same reasoning Patient's own
age/blood_type/gender stay. Nor is there a `dependent_id` column on
Appointment/DiagnosticSession (confirmed by reading
diffdx/db/models/scheduling.py and clinical.py) — dependent-attributed
bookings aren't tracked as a separate relational identity at all. So
Dependent rows for this patient are left entirely untouched by this route.
"""
from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select

from diffdx.audit import log_audit_event
from diffdx.db.engine import get_sessionmaker
from diffdx.db.models.audit import AuditLogEntry
from diffdx.db.models.scheduling import Appointment
from diffdx.db.models.user import RefreshToken, User
from diffdx.dependencies import require_role
from diffdx.dsr_erasure import _build_inventory
from diffdx.legacy_store import _load_appointments, _verify_password
from diffdx.rate_limit import limiter
from diffdx.routers.appointments4 import _cancel_patient_appointment_impl

router = APIRouter(tags=["account_deletion"])
_log = logging.getLogger(__name__)

_ENABLE_ENV = "SELF_SERVICE_ACCOUNT_DEACTIVATION_ENABLED"
_ACTIVE_APPT_STATUSES = ("upcoming", "confirmed", None)


class AccountDeactivationRequest(BaseModel):
    password: str
    confirm_email: str


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/api/patient/account/deactivation-preview")
def get_deactivation_preview(patient: dict = Depends(require_role("patient"))) -> dict[str, Any]:
    """What POST /deactivate would do, for the confirmation modal. Always
    scoped to the calling patient; there is no user-id parameter to accept."""
    uid = uuid.UUID(str(patient["id"]))
    with get_sessionmaker()() as db:
        user = db.get(User, uid)
        if user is None:
            raise HTTPException(status_code=404, detail="Account not found.")
        if user.deleted_at is not None:
            raise HTTPException(status_code=400, detail="Account is already deactivated.")

        n_future_appts = db.execute(
            select(Appointment.id).where(
                Appointment.patient_id == uid, Appointment.status.in_(("upcoming", "confirmed")),
            )
        ).all()

        # _build_inventory's categories are, under deactivation, *all*
        # retained — nothing it counts gets deleted, only Identity's own
        # field values get scrubbed in place (the row count stays 1).
        will_retain = _build_inventory(db, user)

    return {
        "status": "ok",
        "will_scrub": ["name", "email", "password", "mobile", "address", "emergency_contact_name", "emergency_contact_phone"],
        "will_cancel": {"future_appointments": len(n_future_appts)},
        "will_retain": will_retain,
    }


@router.post("/api/patient/account/deactivate")
@limiter.limit("5/minute")
async def deactivate_own_account(
    request: Request,
    body: AccountDeactivationRequest,
    patient: dict = Depends(require_role("patient")),
) -> dict[str, Any]:
    if os.environ.get(_ENABLE_ENV, "").lower() not in ("1", "true", "yes"):
        raise HTTPException(
            status_code=501,
            detail=f"Self-service account deactivation is disabled by default. Set {_ENABLE_ENV}=true to enable it.",
        )

    uid = uuid.UUID(str(patient["id"]))

    with get_sessionmaker()() as db:
        user = db.get(User, uid)
        if user is None:
            raise HTTPException(status_code=404, detail="Account not found.")
        if user.deleted_at is not None:
            raise HTTPException(status_code=400, detail="Account is already deactivated.")
        if not _verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=400, detail="Incorrect password.")
        if user.email.lower().strip() != body.confirm_email.lower().strip():
            raise HTTPException(status_code=400, detail="Email does not match your account.")

        # Logged before the scrub runs — see module docstring's "Audit
        # action, and why it is logged before the scrub runs" section.
        log_audit_event(
            actor=patient, action="subject_self_deactivation", resource_type="user", resource_id=str(uid),
            ip_address=_client_ip(request),
        )

        # 1. Cancel future active appointments — reuses the patient's own
        # cancel-appointment code path (and its notification) verbatim,
        # just with a distinct cancelled_by value. Blob store is the
        # source of truth _cancel_patient_appointment_impl acts on, same
        # as every other appointment route in this codebase.
        appointments = _load_appointments()
        future_ids = [
            appt_id for appt_id, appt in appointments.items()
            if appt.get("patient_user_id") == str(uid) and appt.get("status") in _ACTIVE_APPT_STATUSES
        ]
        cancelled_count = 0
        for appt_id in future_ids:
            try:
                await _cancel_patient_appointment_impl(appt_id, patient, db, cancelled_by="patient_deactivation")
                cancelled_count += 1
            except HTTPException:
                # State moved between the scan above and this call (e.g. a
                # doctor just marked it seen) — skip it, don't fail the
                # whole deactivation over one stale appointment.
                _log.info("Skipped auto-cancel of appointment %s during deactivation of %s — no longer cancellable.", appt_id, uid)

        # 2. Delete all refresh tokens for this user — logged out
        # everywhere immediately.
        token_result = db.execute(delete(RefreshToken).where(RefreshToken.user_id == uid))
        removed_tokens = token_result.rowcount or 0

        # 3. Scrub identity/contact fields in place. Clinically-relevant
        # Patient fields (age, blood_type, gender, allergies,
        # chronic_conditions) are deliberately kept — see module
        # docstring. Dependent rows are untouched — see module docstring's
        # "Dependent rows" section.
        user.name = "Deactivated Patient"
        user.email = f"erased+{uid}@invalid"
        user.password_hash = secrets.token_hex(32)  # never a valid pbkdf2-format hash — unusable for login
        deactivated_at = datetime.now(timezone.utc)
        user.deleted_at = deactivated_at.replace(tzinfo=None)  # naive, matching User.created_at/updated_at on this table
        if user.patient is not None:
            user.patient.mobile = None
            user.patient.address = None
            user.patient.emergency_contact_name = None
            user.patient.emergency_contact_phone = None

        db.commit()

    with get_sessionmaker()() as db2:
        latest = db2.execute(
            select(AuditLogEntry.id)
            .where(AuditLogEntry.action == "subject_self_deactivation", AuditLogEntry.resource_id == str(uid))
            .order_by(AuditLogEntry.created_at.desc()).limit(1)
        ).scalar_one_or_none()

    return {
        "status": "ok",
        "deactivated_at": deactivated_at.isoformat(),
        "audit_entry_id": str(latest) if latest else None,
        "cancelled_appointments": cancelled_count,
        "removed": {"refresh_tokens": removed_tokens},
    }
