"""Continuation of the appointments/doctor/patient domain — patient file
upload/list/delete/download, appointment cancellation/dismiss (both
patient and doctor side), patient-facing doctor-slots lookup, patient
self-reschedule, post-visit rating. Task 4's eleventh split-out router
for this domain. See routers/appointments.py's module docstring for the
overall approach.

_patient_appt_or_403 moved here fully (not lazy-imported) — verified its
only 3 callers were the file upload/list/delete routes, all in this
file; nothing left in web.api would still need it.

Three more auth-pattern quirks preserved exactly here, same reasoning as
appointments3.py:
- download_patient_file accepts EITHER a Bearer header OR a ?token= query
  param (for direct-link downloads that can't attach headers) — a
  meaningfully different, more permissive auth path than
  Depends(get_current_user). Updated for Task 5's JWT switch to decode
  the query-param token the same way as a Bearer one
  (web.api._user_from_access_token), replacing the old opaque
  _TOKENS-dict lookup; the "accept a token via either place" behavior
  itself is preserved.
- patient_dismiss_appointment and patient_reschedule_appointment never
  check `if not user` before using it — a latent crash-on-None risk in
  the original. Not "fixed" here; Task 4 promises zero behavior change,
  bugs included.
- patient_get_doctor_slots calls the auth lookup but discards the
  result entirely (doesn't gate the route on it at all). Preserved as-is.
"""
from __future__ import annotations

import base64
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from diffdx.db.engine import get_session
from diffdx.dependencies import get_current_user, require_role
from diffdx.repositories.appointments import AppointmentRepository
from diffdx.repositories.files import FileRepository
from diffdx.repositories.users import UserRepository
from diffdx.schemas.appointments import PatientRescheduleRequest, RatingRequest
from diffdx.legacy_store import (
    _MAX_FILE_BYTES,
    _ensure_relational_appointment,
    _get_user_from_request,
    _load_appointments,
    _load_doctors,
    _load_file_data,
    _save_appointments,
    _save_doctors,
    _save_file_data,
    _send_email_notification,
    _user_from_access_token,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

router = APIRouter(tags=["appointments"])
_log = logging.getLogger(__name__)


def _patient_appt_or_403(appt_id: str, request: Request) -> tuple[dict, dict]:
    """Return (appointments_dict, appt) verifying the appointment belongs to the caller."""

    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("patient_user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not your appointment.")
    return appointments, appt


@router.post("/api/patient/appointments/{appt_id}/files")
async def upload_patient_file(
    appt_id: str, request: Request,
    file: UploadFile = File(...),
    test_order_id: str | None = None,       # ties file to a doctor-ordered test
    suggested_test_id: str | None = None,   # ties file to an AI-suggested test
    suggested_test_name: str | None = None, # human label for the suggested test
    db: Session = Depends(get_session),
):

    appointments, appt = _patient_appt_or_403(appt_id, request)
    _ALLOWED_UPLOAD_TYPES = {
        "application/pdf", "image/jpeg", "image/png",
        "image/gif", "image/webp", "image/heic",
    }
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_UPLOAD_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Only PDF and image files (JPEG, PNG, GIF, WebP, HEIC) are allowed.",
        )
    raw = await file.read()
    if len(raw) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB).")
    data_b64 = base64.b64encode(raw).decode("ascii")
    # Store file bytes separately so appointments JSON stays small
    _save_file_data(appt_id, file.filename, data_b64)
    uploaded_at = datetime.now(timezone.utc)
    record = {
        "filename": file.filename,
        "size_bytes": len(raw),
        "uploaded_at": uploaded_at.isoformat(),
        "mime_type": file.content_type or "application/octet-stream",
        "test_order_id": test_order_id or None,
        "suggested_test_id": suggested_test_id or None,
        "suggested_test_name": suggested_test_name or None,
    }
    files = appt.setdefault("patient_files", [])
    # Replace any existing file with the same name
    files[:] = [f for f in files if f.get("filename") != file.filename]
    files.append(record)
    # Mark the matched doctor-ordered test as having results uploaded
    if test_order_id:
        for t in appt.get("test_orders", []):
            if t.get("id") == test_order_id:
                t["results_uploaded"] = True
                t["results_filename"] = file.filename
                break
    # Mark the matched suggested test as uploaded
    if suggested_test_id:
        sug_uploads = appt.setdefault("suggested_test_uploads", {})
        sug_uploads[suggested_test_id] = {
            "filename": file.filename,
            "uploaded_at": record["uploaded_at"],
            "test_name": suggested_test_name or suggested_test_id,
        }
    _save_appointments(appointments)

    try:
        appt_uuid = _ensure_relational_appointment(db, appt)
        if appt_uuid is not None:
            dest_dir = _REPO_ROOT / "web" / "data" / "files" / appt_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / file.filename
            dest_path.write_bytes(raw)
            parsed_suggested_test_id: uuid.UUID | None = None
            if suggested_test_id:
                try:
                    parsed_suggested_test_id = uuid.UUID(suggested_test_id)
                except ValueError:
                    parsed_suggested_test_id = None
            FileRepository(db).replace_for_appointment(
                appt_uuid, file.filename,
                storage_path=str(dest_path.relative_to(_REPO_ROOT)),
                content_type=file.content_type,
                suggested_test_id=parsed_suggested_test_id,
                uploaded_at=uploaded_at,
            )
            db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of uploaded file failed for appointment %s", appt_id, exc_info=True)

    return {"saved": True, "filename": file.filename, "size_bytes": len(raw)}


@router.get("/api/patient/appointments/{appt_id}/files")
async def list_patient_files(appt_id: str, request: Request):
    _appointments, appt = _patient_appt_or_403(appt_id, request)
    files = [
        {k: v for k, v in f.items() if k != "data_b64"}
        for f in appt.get("patient_files", [])
    ]
    return {"files": files}


@router.delete("/api/patient/appointments/{appt_id}/files/{filename}")
async def delete_patient_file(appt_id: str, filename: str, request: Request):

    appointments, appt = _patient_appt_or_403(appt_id, request)
    files = appt.get("patient_files", [])
    new_files = [f for f in files if f.get("filename") != filename]
    if len(new_files) == len(files):
        raise HTTPException(status_code=404, detail="File not found.")
    appt["patient_files"] = new_files
    _save_appointments(appointments)
    return {"deleted": True}


@router.get("/api/patient/appointments/{appt_id}/files/{filename}")
async def download_patient_file(appt_id: str, filename: str, request: Request):
    """Download a patient file. Auth accepted via Bearer header or ?token= query for direct links."""

    user = _get_user_from_request(request)
    if not user:
        token = request.query_params.get("token", "")
        if token:
            user = _user_from_access_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    # Allow both the owning patient and the assigned doctor to download
    is_owner = appt.get("patient_user_id") == user["id"]
    is_doctor = user.get("role") == "doctor" and appt.get("doctor_id") == user.get("doctor_id")
    if not (is_owner or is_doctor):
        raise HTTPException(status_code=403, detail="Not permitted.")

    rec = next((f for f in appt.get("patient_files", []) if f.get("filename") == filename), None)
    if rec is None:
        raise HTTPException(status_code=404, detail="File not found.")
    raw_b64 = (
        _load_file_data(appt_id, filename)
        or _load_file_data(f"session:{appt.get('session_id', '')}", filename)
        or rec.get("data_b64", "")
    )
    if not raw_b64:
        raise HTTPException(status_code=404, detail="File data not found.")
    data = base64.b64decode(raw_b64)
    # A raw non-Latin-1 filename (emoji, many non-English scripts — e.g. a
    # phone camera roll name) crashes header construction outright:
    # Response.init_headers encodes header values as latin-1, so this
    # raised an unhandled UnicodeEncodeError (500) with no meaningful
    # message reaching the client. Same fix already applied to the
    # doctor-side equivalent (routers/appointments2.py's
    # doctor_download_patient_file), found live there via a macOS
    # screenshot's narrow no-break space (U+202F) — this route just never
    # got the same treatment.
    safe_name = filename.encode("ascii", "replace").decode("ascii")
    return Response(
        content=data,
        media_type=rec.get("mime_type", "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.delete("/api/patient/appointments/{appt_id}")
async def cancel_patient_appointment(appt_id: str, user: dict = Depends(get_current_user), db: Session = Depends(get_session)):
    """Patient cancels their own upcoming appointment."""

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("patient_user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not your appointment.")
    if appt.get("status") not in ("upcoming", None):
        raise HTTPException(status_code=400, detail="Only upcoming appointments can be cancelled.")
    appt["status"] = "cancelled"
    appt["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    appt["cancelled_by"] = "patient"
    _save_appointments(appointments)
    # Return the slot to the doctor's available pool
    freed_slot = appt.get("slot", "")
    if freed_slot:
        doctors = _load_doctors()
        doc = next((d for d in doctors if d["id"] == appt.get("doctor_id")), None)
        if doc is not None:
            slots = set(doc.get("available_slots", []))
            slots.add(freed_slot)
            doc["available_slots"] = sorted(slots)
            _save_doctors(doctors)

    try:
        appt_uuid = _ensure_relational_appointment(db, appt)
        if appt_uuid is not None:
            AppointmentRepository(db).cancel(appt_uuid, cancelled_by="patient", cancelled_at=datetime.now(timezone.utc))
            db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of cancellation failed for appointment %s", appt_id, exc_info=True)

    _send_email_notification(
        to=user.get("email", ""),
        subject="Appointment Cancelled",
        body=f"Your appointment with {appt.get('doctor_name','your doctor')} on {appt.get('slot','')} has been cancelled.",
    )
    return {"cancelled": True, "freed_slot": freed_slot}


@router.delete("/api/patient/appointments/{appt_id}/dismiss")
async def patient_dismiss_appointment(appt_id: str, request: Request, db: Session = Depends(get_session)):
    """Permanently remove a cancelled or missed appointment from the patient's view.

    Real bug fixed here (see TASK18_APPOINTMENTS_DISMISS_DELETE_FIX.md):
    "permanently remove" must also delete the relational shadow row, not
    just the blob one — get_doctor_appointment_detail (Task 14) does a
    relational-first lookup with no check that the blob still has the
    entry, so a surviving relational row after dismiss made a
    "permanently removed" appointment still fully fetchable by id.
    """

    user = _get_user_from_request(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("patient_user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    now_iso = datetime.now(timezone.utc).isoformat()[:16]
    status = appt.get("status", "")
    slot = appt.get("slot", "")
    is_missed = status == "upcoming" and slot < now_iso
    if status != "cancelled" and not is_missed:
        raise HTTPException(status_code=400, detail="Only cancelled or missed appointments can be deleted.")
    del appointments[appt_id]
    _save_appointments(appointments)

    try:
        appt_uuid = uuid.UUID(appt_id)
        AppointmentRepository(db).delete(appt_uuid)
        db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of dismiss (delete) failed for appointment %s", appt_id, exc_info=True)

    return {"dismissed": True}


@router.delete("/api/doctor/appointments/{appt_id}/dismiss")
async def doctor_dismiss_appointment(
    appt_id: str, doctor: dict = Depends(require_role("doctor")), db: Session = Depends(get_session),
):
    """Permanently remove a cancelled appointment from the doctor's list.

    Real bug fixed here (see TASK18_APPOINTMENTS_DISMISS_DELETE_FIX.md) —
    same reasoning as patient_dismiss_appointment above.
    """

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    if appt.get("status") != "cancelled":
        raise HTTPException(status_code=400, detail="Only cancelled appointments can be removed.")
    del appointments[appt_id]
    _save_appointments(appointments)

    try:
        appt_uuid = uuid.UUID(appt_id)
        AppointmentRepository(db).delete(appt_uuid)
        db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of dismiss (delete) failed for appointment %s", appt_id, exc_info=True)

    return {"dismissed": True}


@router.get("/api/patient/doctors/{doctor_id}/slots")
async def patient_get_doctor_slots(doctor_id: str, request: Request):
    """Return available slots for a doctor — patient-facing, no doctor auth."""

    _get_user_from_request(request)
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == doctor_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doctor not found.")
    now_iso = datetime.now(timezone.utc).isoformat()
    available = [s for s in doc.get("available_slots", []) if s >= now_iso]
    return {"slots": sorted(available), "doctor_name": doc.get("name", "")}


@router.post("/api/patient/appointments/{appt_id}/reschedule")
async def patient_reschedule_appointment(appt_id: str, req: PatientRescheduleRequest, request: Request, db: Session = Depends(get_session)):
    """Cancel old appointment and book same doctor at new_slot."""

    user = _get_user_from_request(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("patient_user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    if appt.get("status") not in (None, "upcoming", "confirmed"):
        raise HTTPException(status_code=400, detail="Only upcoming appointments can be rescheduled.")

    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == appt.get("doctor_id")), None)
    if doc is None:
        raise HTTPException(status_code=400, detail="Doctor not found.")
    if req.new_slot not in doc.get("available_slots", []):
        raise HTTPException(status_code=400, detail="Slot is no longer available.")

    # Mark old appointment cancelled and return its slot
    old_slot = appt.get("slot", "")
    appt["status"] = "cancelled"
    appt["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    appt["cancelled_by"] = "patient_reschedule"
    _save_appointments(appointments)
    if old_slot:
        slots_set = set(doc.get("available_slots", []))
        slots_set.add(old_slot)
        doc["available_slots"] = sorted(slots_set)

    # Remove new slot from doctor's available pool
    available = doc.get("available_slots", [])
    if req.new_slot in available:
        available.remove(req.new_slot)
    doc["available_slots"] = available
    _save_doctors(doctors)

    # Create new appointment
    new_appt_id = str(uuid.uuid4())
    new_appt = {
        "appointment_id": new_appt_id,
        "session_id": appt.get("session_id", ""),
        "patient_user_id": user["id"],
        "patient_name": user.get("name", ""),
        "doctor_id": doc["id"],
        "doctor_name": doc.get("name", ""),
        "specialty": doc.get("specialty", appt.get("specialty", "")),
        "slot": req.new_slot,
        "status": "upcoming",
        "note": req.note or appt.get("note", ""),
        "booked_at": datetime.now(timezone.utc).isoformat(),
        "rescheduled_from": appt_id,
        "is_followup": appt.get("is_followup", False),
        "parent_appointment_id": appt.get("parent_appointment_id", ""),
        "patient_files": appt.get("patient_files", []),
        "primary_diagnosis": appt.get("primary_diagnosis", ""),
        "urgency": appt.get("urgency", "routine"),
        "chief_complaint": appt.get("chief_complaint", ""),
    }
    appointments = _load_appointments()
    appointments[new_appt_id] = new_appt
    _save_appointments(appointments)

    try:
        old_uuid = _ensure_relational_appointment(db, appt)
        doctor_dto = UserRepository(db).get_by_doctor_id(doc["id"])
        if old_uuid is not None and doctor_dto is not None:
            AppointmentRepository(db).cancel(old_uuid, cancelled_by="patient_reschedule", cancelled_at=datetime.now(timezone.utc))
            slot_dt = datetime.fromisoformat(req.new_slot)
            if slot_dt.tzinfo is None:
                slot_dt = slot_dt.replace(tzinfo=timezone.utc)
            AppointmentRepository(db).book(
                id=uuid.UUID(new_appt_id),
                patient_id=uuid.UUID(user["id"]),
                doctor_id=doctor_dto.id,
                slot_datetime=slot_dt,
                urgency=new_appt.get("urgency", "routine"),
                chief_complaint=new_appt.get("chief_complaint") or None,
                primary_diagnosis=new_appt.get("primary_diagnosis") or None,
                note=new_appt.get("note") or None,
                is_followup=new_appt.get("is_followup", False),
                rescheduled_from_id=old_uuid,
            )
            db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of patient reschedule failed for appointment %s", appt_id, exc_info=True)

    return {"rescheduled": True, "new_appt_id": new_appt_id, "new_slot": req.new_slot}


@router.post("/api/patient/appointments/{appt_id}/rating")
async def submit_rating(appt_id: str, req: RatingRequest, user: dict = Depends(get_current_user), db: Session = Depends(get_session)):
    """Patient submits a 1-5 star rating after a visit."""

    if not (1 <= req.rating <= 5):
        raise HTTPException(status_code=400, detail="Rating must be 1-5.")
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("patient_user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not your appointment.")
    if appt.get("status") != "seen":
        raise HTTPException(status_code=400, detail="Can only rate completed visits.")
    submitted_at = datetime.now(timezone.utc)
    appt["rating"] = {"stars": req.rating, "comment": req.comment, "submitted_at": submitted_at.isoformat()}
    _save_appointments(appointments)

    try:
        appt_uuid = _ensure_relational_appointment(db, appt)
        if appt_uuid is not None:
            AppointmentRepository(db).set_rating(appt_uuid, stars=req.rating, comment=req.comment, submitted_at=submitted_at)
            db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of rating failed for appointment %s", appt_id, exc_info=True)

    return {"saved": True}
