"""/api/appointments, /api/patient/test-notifications, and the core
doctor-side appointment-mutation routes (tests, files, referral, notes,
status, reschedule, propose-reschedule) plus the one patient-side route
interleaved among them (reschedule-response). Task 4's eighth split-out
router — part 1 of several for this domain, since it's far too big for
one 400-line file (~50 routes total across appointments/doctor/patient).
See TASK4_SPLIT_ROUTERS.md. Continues in appointments2.py (plan,
test-results, and onward).

Unlike earlier domains, this one is NOT cleanly separable into "doctor"
vs "patient" vs "appointments" files — the original code interleaves
doctor and patient actions on the same appointment lifecycle throughout.
Rather than force a semantic regrouping (real risk of subtle mistakes
reordering 50 tightly-coupled routes), this and the files that follow it
split by physically contiguous chunks of ~350-390 lines, preserving the
original order exactly. Each file's docstring names its route range.

_require_doctor(request) -> Depends(require_role("doctor")) verified
equivalent (see routers/doctors.py). Manual _get_user_from_request +
401 check -> Depends(get_current_user), same simplification as every
other domain.
"""
from __future__ import annotations

import base64
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from diffdx.db.engine import get_session
from diffdx.dependencies import get_current_user, require_role
from diffdx.repositories.appointments import AppointmentRepository
from diffdx.repositories.clinical import ReferralRepository, SuggestedTestRepository
from diffdx.repositories.files import FileRepository
from diffdx.repositories.users import UserRepository

_REPO_ROOT = Path(__file__).resolve().parents[3]
from diffdx.schemas.appointments import (
    NotesRequest,
    ProposeRescheduleRequest,
    ReferralRequest,
    RescheduleRequest,
    RescheduleResponseRequest,
    StatusRequest,
    TestOrdersRequest,
)

router = APIRouter(tags=["appointments"])
_log = logging.getLogger(__name__)


@router.get("/api/appointments")
async def get_patient_appointments(user: dict = Depends(get_current_user), db: Session = Depends(get_session)):
    """Return all booked appointments for the authenticated patient.

    Phase C of the appointments cutover (first list-route flip, see
    TASK15_APPOINTMENTS_FLIP_LIST_ROUTE.md): the blob remains the
    authoritative source for *which* appointments exist (every booking
    still writes one, dual-write or not), but each entry is composed from
    the relational store when a row exists for it, falling back to the
    raw blob record otherwise — same per-item fallback principle as
    get_doctor_appointment_detail (Task 14), applied across a list.
    """
    from web.api import _compose_appointment_dict, _load_appointments

    composed_by_id = {
        str(dto.id): _compose_appointment_dict(db, dto)
        for dto in AppointmentRepository(db).list_for_patient(uuid.UUID(user["id"]))
    }
    appointments = _load_appointments()
    patient_appts = []
    for appt_id, a in appointments.items():
        if a.get("patient_user_id") != user["id"]:
            continue
        entry = dict(composed_by_id.get(appt_id, a))
        # Strip doctor-only fields before sending to patient
        if "referral" in entry and "internal_note" in entry["referral"]:
            entry["referral"] = {k: v for k, v in entry["referral"].items() if k != "internal_note"}
        patient_appts.append(entry)
    patient_appts.sort(key=lambda a: a.get("slot", ""), reverse=True)
    return {"appointments": patient_appts}


@router.get("/api/patient/test-notifications")
async def get_test_notifications(request: Request, db: Session = Depends(get_session)):
    """Return pending test order count for the logged-in patient (used for nav badge).

    Phase C of the appointments cutover (see
    TASK17_APPOINTMENTS_FLIP_DERIVED_ROUTES.md): the filter (`test_orders`
    truthy) stays blob-truth, same principle as every prior flip — only
    the doctor_name/slot/session_id read for matched entries is
    substituted with composed data where available. Preserves a
    pre-existing, unrelated bug found while doing this: `"appt_id":
    a.get("id")` has always been None (blob records only ever have
    "appointment_id", never "id") — not fixed, a composed dict doesn't
    have an "id" key either, so this stays exactly as broken as before.
    """
    from web.api import _compose_appointment_dict, _get_user_from_request, _load_appointments

    user = _get_user_from_request(request)
    if not user:
        return {"count": 0, "appointments": []}
    composed_by_id = {
        str(dto.id): _compose_appointment_dict(db, dto)
        for dto in AppointmentRepository(db).list_for_patient(uuid.UUID(user["id"]))
    }
    appointments = _load_appointments()
    pending = [
        {"appt_id": a.get("id"), "doctor_name": entry.get("doctor_name"), "slot": entry.get("slot"),
         "session_id": entry.get("session_id"), "test_count": len(a.get("test_orders", [])),
         "has_stat": any(t.get("priority") == "stat" for t in a.get("test_orders", []))}
        for appt_id, a in appointments.items()
        if a.get("patient_user_id") == user["id"] and a.get("test_orders")
        for entry in [composed_by_id.get(appt_id, a)]
    ]
    return {"count": sum(p["test_count"] for p in pending), "appointments": pending}


@router.get("/api/doctor/appointments")
async def get_doctor_appointments(doctor: dict = Depends(require_role("doctor")), db: Session = Depends(get_session)):
    """List all appointments assigned to this doctor.

    Phase C of the appointments cutover (see
    TASK16_APPOINTMENTS_FLIP_DOCTOR_ROUTES.md): same per-item
    relational-or-blob-fallback pattern as get_patient_appointments
    (Task 15) — the blob stays authoritative for which appointments exist
    for this doctor_id, composed data is substituted in per-item where a
    relational row exists.
    """
    from web.api import _compose_appointment_dict, _load_appointments

    doctor_id = doctor.get("doctor_id")
    composed_by_id = {}
    doctor_dto = UserRepository(db).get_by_doctor_id(doctor_id) if doctor_id else None
    if doctor_dto is not None:
        composed_by_id = {
            str(dto.id): _compose_appointment_dict(db, dto)
            for dto in AppointmentRepository(db).list_for_doctor(doctor_dto.id)
        }
    appointments = _load_appointments()
    mine = [
        composed_by_id.get(appt_id, a)
        for appt_id, a in appointments.items()
        if a.get("doctor_id") == doctor_id
    ]
    mine.sort(key=lambda a: a.get("slot", ""))
    return mine


@router.patch("/api/doctor/appointments/{appt_id}/tests")
async def update_test_orders(
    appt_id: str, req: TestOrdersRequest,
    doctor: dict = Depends(require_role("doctor")), db: Session = Depends(get_session),
):
    """Save the doctor's test orders for an appointment."""
    from web.api import _ensure_relational_appointment, _load_appointments, _save_appointments

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["test_orders"] = [t.model_dump() for t in req.test_orders]
    appt["tests_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)

    try:
        appt_uuid = _ensure_relational_appointment(db, appt)
        if appt_uuid is not None:
            SuggestedTestRepository(db).replace_for_appointment(appt_uuid, appt["test_orders"])
            db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of test orders failed for appointment %s", appt_id, exc_info=True)

    return {"saved": True, "count": len(req.test_orders)}


@router.post("/api/doctor/appointments/{appt_id}/files")
async def doctor_upload_file(
    appt_id: str,
    file: UploadFile = File(...),
    test_order_id: str | None = None,
    doctor: dict = Depends(require_role("doctor")),
    db: Session = Depends(get_session),
):
    """Doctor uploads a result file for an appointment (e.g. lab report PDF)."""
    from web.api import _MAX_FILE_BYTES, _ensure_relational_appointment, _load_appointments, _save_appointments, _save_file_data

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    _ALLOWED = {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp", "image/heic"}
    ct = (file.content_type or "").split(";")[0].strip().lower()
    if ct not in _ALLOWED:
        raise HTTPException(status_code=415, detail="Only PDF and image files are allowed.")
    raw = await file.read()
    if len(raw) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB).")
    data_b64 = base64.b64encode(raw).decode("ascii")
    _save_file_data(appt_id, file.filename, data_b64)
    uploaded_at = datetime.now(timezone.utc)
    record = {
        "filename": file.filename,
        "size_bytes": len(raw),
        "uploaded_at": uploaded_at.isoformat(),
        "mime_type": file.content_type or "application/octet-stream",
        "uploaded_by": "doctor",
        "test_order_id": test_order_id or None,
    }
    files = appt.setdefault("patient_files", [])
    files[:] = [f for f in files if f.get("filename") != file.filename]
    files.append(record)
    if test_order_id:
        for t in appt.get("test_orders", []):
            if t.get("id") == test_order_id:
                t["results_uploaded"] = True
                t["results_filename"] = file.filename
                break
    _save_appointments(appointments)

    try:
        appt_uuid = _ensure_relational_appointment(db, appt)
        if appt_uuid is not None:
            dest_dir = _REPO_ROOT / "web" / "data" / "files" / appt_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / file.filename
            dest_path.write_bytes(raw)
            FileRepository(db).replace_for_appointment(
                appt_uuid, file.filename,
                storage_path=str(dest_path.relative_to(_REPO_ROOT)),
                content_type=file.content_type,
                uploaded_at=uploaded_at,
            )
            db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of uploaded file failed for appointment %s", appt_id, exc_info=True)

    return {"saved": True, "filename": file.filename, "size_bytes": len(raw)}


@router.post("/api/doctor/appointments/{appt_id}/referral")
async def save_referral(
    appt_id: str, req: ReferralRequest,
    doctor: dict = Depends(require_role("doctor")), db: Session = Depends(get_session),
):
    """Save a referral issued by the doctor for this appointment."""
    from web.api import _ensure_relational_appointment, _load_appointments, _save_appointments

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["referral"] = {
        "specialty": req.specialty,
        "to_doctor": req.to_doctor,
        "urgency": req.urgency,
        "notes": req.notes,
        "internal_note": req.internal_note,   # stored but stripped from patient-facing GET /api/appointments
        "referred_at": req.referred_at or datetime.now(timezone.utc).isoformat(),
        "referring_doctor": doctor.get("name", ""),
    }
    _save_appointments(appointments)

    try:
        appt_uuid = _ensure_relational_appointment(db, appt)
        if appt_uuid is not None:
            ReferralRepository(db).upsert(
                appt_uuid, specialty=req.specialty, to_doctor=req.to_doctor,
                urgency=req.urgency, notes=req.notes, internal_note=req.internal_note,
            )
            db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of referral failed for appointment %s", appt_id, exc_info=True)

    return {"saved": True}


@router.patch("/api/doctor/appointments/{appt_id}/notes")
async def update_doctor_notes(appt_id: str, req: NotesRequest, doctor: dict = Depends(require_role("doctor"))):
    """Save doctor's free-text notes on an appointment."""
    from web.api import _load_appointments, _save_appointments

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["doctor_notes"] = req.notes
    appt["notes_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)
    return {"saved": True}


@router.patch("/api/doctor/appointments/{appt_id}/status")
async def update_appointment_status(
    appt_id: str, req: StatusRequest,
    doctor: dict = Depends(require_role("doctor")), db: Session = Depends(get_session),
):
    """Update appointment status: upcoming | seen | no_show."""
    from web.api import (
        _ensure_relational_appointment,
        _load_appointments,
        _load_users,
        _load_waitlist,
        _save_appointments,
        _save_waitlist,
        _send_email_notification,
    )

    valid = {"upcoming", "seen", "no_show"}
    if req.status not in valid:
        raise HTTPException(status_code=400, detail=f"Status must be one of: {valid}")
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    old_status = appt.get("status", "upcoming")
    appt["status"] = req.status
    appt["status_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)

    try:
        appt_uuid = _ensure_relational_appointment(db, appt)
        if appt_uuid is not None:
            AppointmentRepository(db).update_status(appt_uuid, req.status)
            db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of status failed for appointment %s", appt_id, exc_info=True)

    # Waitlist notification: when appointment is cancelled, notify first waiting patient
    if req.status == "cancelled" and old_status != "cancelled":
        doctor_id = appt.get("doctor_id", "")
        if doctor_id:
            waitlist = _load_waitlist()
            waiting = [e for e in waitlist if e.get("doctor_id") == doctor_id and e.get("status") == "waiting"]
            if waiting:
                first = waiting[0]
                first["status"] = "notified"
                _save_waitlist(waitlist)
                # Email the waiting patient
                users = _load_users()
                patient = users.get(first.get("patient_user_id", ""), {})
                p_email = patient.get("email", "")
                p_name = patient.get("name", "Patient")
                _send_email_notification(
                    to=p_email,
                    subject=f"Slot Available — {first.get('doctor_name', 'Your doctor')}",
                    body=(
                        f"Hi {p_name},\n\n"
                        f"Good news! A slot has opened up with {first.get('doctor_name', 'your doctor')} ({first.get('specialty', '')}).\n"
                        f"Please log in to DiffDx to book your appointment before it fills up.\n"
                    ),
                )

    return {"status": req.status}


@router.patch("/api/doctor/appointments/{appt_id}/reschedule")
async def reschedule_appointment(
    appt_id: str, req: RescheduleRequest,
    doctor: dict = Depends(require_role("doctor")), db: Session = Depends(get_session),
):
    """Reschedule an appointment to a new slot."""
    from web.api import (
        _ensure_relational_appointment,
        _load_appointments,
        _load_blocked_dates,
        _load_doctors,
        _save_appointments,
        _save_doctors,
    )

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == appt["doctor_id"]), None)
    if doc is None or req.slot not in doc.get("available_slots", []):
        raise HTTPException(status_code=400, detail="Slot not available.")
    blocked_data = _load_blocked_dates()
    blocked_dates = {d["date"] for d in blocked_data.get(appt["doctor_id"], [])}
    if req.slot[:10] in blocked_dates:
        raise HTTPException(status_code=400, detail="That date is blocked on the doctor's calendar.")
    if appt.get("reschedule_proposal", {}).get("status") == "pending":
        raise HTTPException(status_code=409, detail="A reschedule proposal is pending patient response. Cancel or wait for it to resolve first.")
    old_slot = appt.get("slot")
    appt["slot"] = req.slot
    appt["rescheduled_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)
    # Remove the new slot from available; add the old one back
    slots = set(doc.get("available_slots", []))
    slots.discard(req.slot)
    if old_slot:
        slots.add(old_slot)
    doc["available_slots"] = sorted(slots)
    _save_doctors(doctors)

    try:
        appt_uuid = _ensure_relational_appointment(db, appt)
        if appt_uuid is not None:
            slot_dt = datetime.fromisoformat(req.slot)
            if slot_dt.tzinfo is None:
                slot_dt = slot_dt.replace(tzinfo=timezone.utc)
            AppointmentRepository(db).reschedule(appt_uuid, slot_dt)
            db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of reschedule failed for appointment %s", appt_id, exc_info=True)

    return {"slot": req.slot}


@router.post("/api/doctor/appointments/{appt_id}/propose-reschedule")
async def propose_reschedule(appt_id: str, req: ProposeRescheduleRequest, doctor: dict = Depends(require_role("doctor"))):
    """Doctor proposes a new slot to the patient; patient must accept or decline."""
    from web.api import (
        _load_appointments,
        _load_blocked_dates,
        _load_users,
        _save_appointments,
        _send_email_notification,
    )

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    blocked_data = _load_blocked_dates()
    blocked_dates = {d["date"] for d in blocked_data.get(doctor.get("doctor_id", ""), [])}
    if req.proposed_slot[:10] in blocked_dates:
        raise HTTPException(status_code=400, detail="That date is blocked on your calendar.")
    appt["reschedule_proposal"] = {
        "proposed_slot": req.proposed_slot,
        "reason": req.reason,
        "status": "pending",
        "proposed_at": datetime.now(timezone.utc).isoformat(),
        "proposed_by": doctor.get("name", "Your doctor"),
    }
    _save_appointments(appointments)
    # Notify patient by email
    users = _load_users()
    patient = next((u for u in users.values() if u.get("id") == appt.get("patient_user_id")), None)
    if patient:
        old_slot = (appt.get("slot") or "").replace("T", " at ").replace(":00", "")
        new_slot = req.proposed_slot.replace("T", " at ").replace(":00", "")
        _send_email_notification(
            to=patient.get("email", ""),
            subject="Appointment Reschedule Proposed",
            body=f"Hi {patient.get('name','')},\n\n{doctor.get('name','Your doctor')} has proposed to reschedule your appointment.\n\nCurrent slot: {old_slot}\nProposed slot: {new_slot}\nReason: {req.reason or 'Date unavailable'}\n\nPlease log in and accept the new slot or book with another doctor.",
        )
    return {"status": "proposed"}


@router.patch("/api/patient/appointments/{appt_id}/reschedule-response")
async def patient_reschedule_response(
    appt_id: str, req: RescheduleResponseRequest,
    user: dict = Depends(get_current_user), db: Session = Depends(get_session),
):
    """Patient accepts or declines a doctor's reschedule proposal."""
    from web.api import (
        _ensure_relational_appointment,
        _load_appointments,
        _load_doctors,
        _load_users,
        _save_appointments,
        _save_doctors,
        _send_email_notification,
    )

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("patient_user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    proposal = appt.get("reschedule_proposal")
    if not proposal or proposal.get("status") != "pending":
        raise HTTPException(status_code=400, detail="No pending reschedule proposal.")
    accepted = req.action == "accept"
    if accepted:
        old_slot = appt.get("slot")
        new_slot = proposal["proposed_slot"]
        appt["slot"] = new_slot
        appt["rescheduled_at"] = datetime.now(timezone.utc).isoformat()
        appt["reschedule_proposal"]["status"] = "accepted"
        # Swap slots on doctor record
        doctors = _load_doctors()
        doc = next((d for d in doctors if d["id"] == appt.get("doctor_id")), None)
        if doc:
            slots = set(doc.get("available_slots", []))
            slots.discard(new_slot)
            if old_slot:
                slots.add(old_slot)
            doc["available_slots"] = sorted(slots)
            _save_doctors(doctors)
    elif req.action == "decline":
        appt["reschedule_proposal"]["status"] = "declined"
    else:
        raise HTTPException(status_code=400, detail="action must be accept or decline.")
    _save_appointments(appointments)

    if accepted:
        try:
            appt_uuid = _ensure_relational_appointment(db, appt)
            if appt_uuid is not None:
                slot_dt = datetime.fromisoformat(appt["slot"])
                if slot_dt.tzinfo is None:
                    slot_dt = slot_dt.replace(tzinfo=timezone.utc)
                AppointmentRepository(db).reschedule(appt_uuid, slot_dt)
                db.commit()
        except Exception:
            db.rollback()
            _log.warning("Dual-write of reschedule-response failed for appointment %s", appt_id, exc_info=True)
    # Notify doctor
    users = _load_users()
    doctor_user = next((u for u in users.values() if u.get("doctor_id") == appt.get("doctor_id")), None)
    if doctor_user:
        action_label = "accepted" if req.action == "accept" else "declined"
        _send_email_notification(
            to=doctor_user.get("email", ""),
            subject=f"Reschedule {action_label.capitalize()} by Patient",
            body=f"Hi {doctor_user.get('name','')},\n\n{appt.get('patient_name','Your patient')} has {action_label} the reschedule proposal for their appointment.",
        )
    return {"status": req.action}

