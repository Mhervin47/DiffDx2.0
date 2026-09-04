"""Continuation of routers/appointments.py — plan, test-results (deferred
from part 1 to keep it under 400 lines), doctor file download,
patient-history, prescriptions, followup, summary, single-appointment
detail, doctor profile/profile-details/slots. Task 4's ninth split-out
router for this domain. See appointments.py's module docstring for the
overall approach and TASK4_SPLIT_ROUTERS.md for the full picture.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from diffdx.db.engine import get_session
from diffdx.dependencies import require_role
from diffdx.repositories.users import UserRepository
from diffdx.schemas.appointments import (
    ApprovedPlanRequest,
    DoctorSummaryRequest,
    FollowUpRequest,
    PrescriptionsRequest,
    SlotsRequest,
    TestResultsRequest,
)

router = APIRouter(tags=["appointments"])


@router.patch("/api/doctor/appointments/{appt_id}/plan")
async def update_approved_plan(appt_id: str, req: ApprovedPlanRequest, doctor: dict = Depends(require_role("doctor"))):
    """Save the doctor's approved / modified AI plan for an appointment."""
    from web.api import _load_appointments, _save_appointments

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["approved_plan"] = [p.model_dump() for p in req.plan]
    appt["plan_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)
    return {"saved": True, "count": len(req.plan)}


@router.patch("/api/doctor/appointments/{appt_id}/test-results")
async def update_test_results(appt_id: str, req: TestResultsRequest, doctor: dict = Depends(require_role("doctor"))):
    """Save lab results against test orders for an appointment."""
    from web.api import _load_appointments, _save_appointments

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["test_results_data"] = [r.model_dump() for r in req.test_results]
    appt["results_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)
    return {"saved": True, "count": len(req.test_results)}


@router.get("/api/doctor/appointments/{appt_id}/file")
async def doctor_download_patient_file(appt_id: str, filename: str = "", doctor: dict = Depends(require_role("doctor"))):
    """Serve a patient-uploaded file to the doctor assigned to that appointment.
    Filename is passed as a query param (?filename=...) to avoid Starlette path-decode
    issues with non-ASCII characters (e.g. macOS screenshot narrow no-break space U+202F)."""
    from web.api import _load_appointments, _load_file_data

    if not filename:
        raise HTTPException(status_code=400, detail="filename query param required.")
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
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
    safe_name = filename.encode("ascii", "replace").decode("ascii")
    return Response(
        content=data,
        media_type=rec.get("mime_type", "application/octet-stream"),
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


@router.get("/api/doctor/patient-history/{patient_name}")
async def get_patient_history(patient_name: str, exclude: str = "", doctor: dict = Depends(require_role("doctor"))):
    """Return this doctor's own past appointments with a patient (case-insensitive
    name match), sorted newest first.

    IDOR fix (Task 5 audit): the original had no ownership check at all —
    any authenticated doctor could pull any patient's full appointment
    history (diagnoses, prescriptions, notes) just by knowing or guessing
    their name, since there wasn't even an appt_id to scope against. Scoped
    to `doctor_id == doctor.get("doctor_id")` to match the actual product
    intent (the drawer is opened from a doctor's own appointment view to see
    their own prior visits with this patient) and to close the PHI leak.
    """
    from web.api import _load_appointments

    appointments = _load_appointments()
    doctor_id = doctor.get("doctor_id")
    history = [
        a for a in appointments.values()
        if a.get("patient_name", "").lower() == patient_name.lower()
        and a.get("appointment_id") != exclude
        and a.get("doctor_id") == doctor_id
    ]
    history.sort(key=lambda a: a.get("slot", ""), reverse=True)
    return {"history": history}


@router.patch("/api/doctor/appointments/{appt_id}/prescriptions")
async def update_prescriptions(appt_id: str, req: PrescriptionsRequest, doctor: dict = Depends(require_role("doctor"))):
    """Save prescription pad for an appointment."""
    from web.api import _load_appointments, _save_appointments

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    now_iso = datetime.now(timezone.utc).isoformat()
    new_rx = [p.model_dump() for p in req.prescriptions]
    # Maintain history: update last batch in-place if saved < 10 min ago AND same IDs, else append
    history = appt.get("prescription_history", [])
    def _rx_ids(batch): return {p.get("id") for p in batch.get("prescriptions", [])}
    new_ids = {p.id for p in req.prescriptions}
    if not req.force_new_batch and history:
        last_saved = history[-1].get("saved_at", "")
        try:
            age_s = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(last_saved)).total_seconds()
        except Exception:
            age_s = 999
        same_items = _rx_ids(history[-1]) == new_ids
        if age_s < 600 and same_items:
            history[-1]["prescriptions"] = new_rx
            history[-1]["saved_at"] = now_iso
        else:
            history.append({"saved_at": now_iso, "prescriptions": new_rx})
    else:
        history.append({"saved_at": now_iso, "prescriptions": new_rx})
    appt["prescription_history"] = history
    appt["prescriptions"] = new_rx  # keep for backward compat
    appt["prescriptions_updated_at"] = now_iso
    _save_appointments(appointments)
    return {"saved": True, "count": len(new_rx)}


@router.post("/api/doctor/appointments/{appt_id}/followup")
async def create_followup(appt_id: str, req: FollowUpRequest, doctor: dict = Depends(require_role("doctor"))):
    """Create a follow-up appointment linked to an existing appointment."""
    from web.api import _load_appointments, _load_doctors, _save_appointments, _save_doctors

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    # Validate slot
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == appt.get("doctor_id")), None)
    if doc is None or req.slot not in doc.get("available_slots", []):
        raise HTTPException(status_code=400, detail="Slot not available.")
    # Remove slot from doctor's available slots
    doc["available_slots"] = [s for s in doc["available_slots"] if s != req.slot]
    _save_doctors(doctors)
    # Create follow-up appointment record
    followup_id = str(uuid.uuid4())
    followup = {
        "appointment_id": followup_id,
        "session_id": "",
        "patient_user_id": appt.get("patient_user_id", ""),
        "patient_name": appt.get("patient_name", ""),
        "doctor_id": appt.get("doctor_id"),
        "doctor_name": appt.get("doctor_name", ""),
        "specialty": appt.get("specialty", ""),
        "slot": req.slot,
        "booked_at": datetime.now(timezone.utc).isoformat(),
        "primary_diagnosis": appt.get("primary_diagnosis", ""),
        "urgency": appt.get("urgency", "routine"),
        "status": "upcoming",
        "is_followup": True,
        "parent_appointment_id": appt_id,
        "followup_notes": req.notes,
    }
    appointments[followup_id] = followup
    _save_appointments(appointments)
    return {"followup_appointment_id": followup_id, "slot": req.slot}


@router.patch("/api/doctor/appointments/{appt_id}/summary")
async def update_doctor_summary(appt_id: str, req: DoctorSummaryRequest, doctor: dict = Depends(require_role("doctor"))):
    """Save a doctor's plain-language summary for the patient."""
    from web.api import _load_appointments, _save_appointments

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["doctor_summary"] = req.summary
    appt["summary_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)
    return {"saved": True}


@router.get("/api/doctor/appointments/{appt_id}")
async def get_doctor_appointment_detail(appt_id: str, doctor: dict = Depends(require_role("doctor"))):
    """Full appointment detail: patient info + AI session data.

    IDOR fix (Task 5 audit): the docstring used to say "any authenticated
    doctor may view" as a deliberate design choice, but that means any
    doctor account can pull any patient's full AI diagnostic conversation
    and session data — a real PHI leak, not a feature. Now requires the
    requesting doctor to either own the appointment, or be the recipient
    of a pending/responded second-opinion request on it (the one
    legitimate cross-doctor use case this domain has — see
    appointments6.py's request_second_opinion).
    """
    from loop3.routing.router import route as compute_routing
    from web.api import (
        _get_final_differential,
        _load_appointments,
        _load_doctors,
        _load_report_from_disk,
        _repo_root,
        _sessions,
    )

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    doctor_id = doctor.get("doctor_id")
    is_owner = appt.get("doctor_id") == doctor_id
    is_second_opinion_recipient = (appt.get("second_opinion") or {}).get("to_doctor_id") == doctor_id
    if not is_owner and not is_second_opinion_recipient:
        raise HTTPException(status_code=403, detail="Not your appointment.")

    session_id = appt["session_id"]

    # Load session report
    session = _sessions.get(session_id)
    if session and session.complete:
        report = session.get_report()
    else:
        report = _load_report_from_disk(session_id)

    # Load routing
    diff, confidence = _get_final_differential(session_id)
    routing_data = None
    if diff:
        rd = compute_routing(session_id, diff, confidence)
        routing_data = {
            "specialty": rd.primary_routing.specialty,
            "urgency": rd.primary_routing.urgency.value,
            "appointment_type": rd.primary_routing.appointment_type.value,
            "reasoning": rd.primary_routing.reasoning,
            "is_emergency": rd.primary_routing.urgency.value == "emergency",
            "is_ambiguous": rd.is_ambiguous,
            "final_confidence": rd.final_confidence,
        }

    # Load full turn history — prefer DB report (works on Render), fall back to disk
    turn_history = []
    if report and report.get("turn_history"):
        turn_history = report["turn_history"]
    else:
        jsonl_path = _repo_root / "logs" / f"session_{session_id}.jsonl"
        if jsonl_path.exists():
            try:
                lines = jsonl_path.read_text(encoding="utf-8").splitlines()
                for line in lines:
                    if line.strip():
                        turn_history.append(json.loads(line))
            except Exception:
                pass

    # Include doctor's available slots so the frontend can open a reschedule calendar
    # without needing a separate API call (avoids StaticFiles catch-all conflicts)
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == appt.get("doctor_id")), None)
    now_prefix = datetime.now(timezone.utc).isoformat()[:16]
    doctor_slots = [s for s in doc.get("available_slots", []) if s >= now_prefix] if doc else []

    return {
        "appointment": appt,
        "session_report": report,
        "routing": routing_data,
        "turn_history": turn_history,
        "doctor_slots": doctor_slots,
    }


@router.get("/api/doctor/profile")
async def get_doctor_profile(doctor_user: dict = Depends(require_role("doctor"))):
    """Return the authenticated doctor's profile including available_slots."""
    from web.api import _load_doctors

    doctor_id = doctor_user.get("doctor_id")
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == doctor_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doctor record not found.")
    return {"doctor": doc}


@router.patch("/api/doctor/profile-details")
async def update_doctor_profile_details(
    request: Request,
    doctor_user: dict = Depends(require_role("doctor")),
    db: Session = Depends(get_session),
):
    """Save professional, education, and bio details for the authenticated doctor."""
    from web.api import _load_doctors, _save_doctors

    doctor_id = doctor_user.get("doctor_id")
    body = await request.json()

    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == doctor_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doctor record not found.")

    # Top-level fields mirrored to doc root for easy display elsewhere
    if "specialty" in body:
        doc["specialty"] = body["specialty"]
    if "hospital" in body:
        doc["hospital"] = body["hospital"]

    # Nested profile block
    prof = doc.setdefault("profile", {})
    for field in ("mobile", "experience", "license", "fee", "bio"):
        if field in body:
            prof[field] = body[field]
    if "education" in body:
        prof["education"] = body["education"]

    _save_doctors(doctors)

    # Mirror name/specialty/hospital into the relational Doctor row too —
    # /api/auth/me and login responses read from there, not the directory
    # blob above, so without this they'd drift stale relative to this edit.
    doctor_fields = {k: body[k] for k in ("specialty", "hospital") if k in body}
    name = body.get("name") or None
    if doctor_fields or name:
        UserRepository(db).update_doctor(uuid.UUID(doctor_user["id"]), name=name, **doctor_fields)
        db.commit()

    return {"saved": True}


@router.patch("/api/doctor/slots")
async def update_doctor_slots(req: SlotsRequest, doctor_user: dict = Depends(require_role("doctor"))):
    """Replace the authenticated doctor's available_slots list."""
    from web.api import _load_doctors, _save_doctors

    doctor_id = doctor_user.get("doctor_id")
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == doctor_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doctor record not found.")
    doc["available_slots"] = sorted(set(req.slots))
    _save_doctors(doctors)
    return {"saved": True, "count": len(doc["available_slots"])}
