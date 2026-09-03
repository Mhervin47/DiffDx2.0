"""Second-opinion request/inbox/respond — split out of appointments5.py to
stay under 400 lines (that file exceeded it once this domain was fully
combined; same reasoning as sessions.py/session_booking.py's earlier
split). Task 4's thirteenth and truly last split-out router for the
appointments/doctor/patient domain.

_load_second_opinions/_save_second_opinions moved here fully (not
lazy-imported) — verified these are the only three routes that call them.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from diffdx.schemas.appointments import (
    SecondOpinionRequest,
    SecondOpinionResponseRequest,
)

router = APIRouter(tags=["appointments"])


def _load_second_opinions() -> list:
    from web.api import _db_load

    return _db_load("second_opinions", [])


def _save_second_opinions(opinions: list) -> None:
    from web.api import _db_save

    _db_save("second_opinions", opinions)


@router.post("/api/doctor/appointments/{appt_id}/second-opinion")
async def request_second_opinion(appt_id: str, req: SecondOpinionRequest, request: Request):
    from web.api import (
        _load_appointments,
        _load_users,
        _require_doctor,
        _save_appointments,
        _send_email_notification,
    )

    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")

    opinions = _load_second_opinions()
    opinion_id = str(uuid.uuid4())
    now_str = datetime.now(timezone.utc).isoformat()

    diagnosis = appt.get("primary_diagnosis", "")
    patient_summary = f"Diagnosis: {diagnosis}" if diagnosis else "No diagnosis yet"

    opinion = {
        "id": opinion_id,
        "from_doctor_id": doctor.get("doctor_id"),
        "from_doctor_name": doctor.get("name", ""),
        "to_doctor_id": req.to_doctor_id,
        "to_doctor_name": req.to_doctor_name,
        "appointment_id": appt_id,
        "patient_name": appt.get("patient_name", ""),
        "patient_summary": patient_summary,
        "note": req.note,
        "requested_at": now_str,
        "status": "pending",
        "response": "",
    }
    opinions.append(opinion)
    _save_second_opinions(opinions)

    appt["second_opinion"] = {
        "to_doctor_id": req.to_doctor_id,
        "to_doctor_name": req.to_doctor_name,
        "requested_at": now_str,
        "status": "pending",
        "opinion_id": opinion_id,
    }
    _save_appointments(appointments)

    users = _load_users()
    to_doctor_user = next(
        (u for u in users.values() if u.get("doctor_id") == req.to_doctor_id),
        None,
    )
    if to_doctor_user:
        _send_email_notification(
            to=to_doctor_user.get("email", ""),
            subject=f"Second Opinion Request — {appt.get('patient_name', 'Patient')}",
            body=(
                f"Hi {req.to_doctor_name},\n\n"
                f"Dr. {doctor.get('name', '')} is requesting a second opinion on a patient.\n\n"
                f"Patient: {appt.get('patient_name', '')}\n"
                f"Summary: {patient_summary}\n"
                + (f"Note: {req.note}\n" if req.note else "")
                + f"\nPlease log in to the doctor portal to review and respond.\n"
            ),
        )

    return {"requested": True, "opinion_id": opinion_id}


@router.get("/api/doctor/second-opinions/inbox")
async def get_second_opinion_inbox(request: Request):
    """Return second opinion requests sent TO this doctor."""
    from web.api import _require_doctor

    doctor = _require_doctor(request)
    doctor_id = doctor.get("doctor_id")
    opinions = _load_second_opinions()
    inbox = [o for o in opinions if o.get("to_doctor_id") == doctor_id]
    inbox.sort(key=lambda o: o.get("requested_at", ""), reverse=True)
    return {"inbox": inbox, "count": len(inbox)}


@router.patch("/api/doctor/second-opinions/{opinion_id}/respond")
async def respond_to_second_opinion(opinion_id: str, req: SecondOpinionResponseRequest, request: Request):
    from web.api import (
        _load_appointments,
        _load_users,
        _require_doctor,
        _save_appointments,
        _send_email_notification,
    )

    doctor = _require_doctor(request)
    opinions = _load_second_opinions()
    opinion = next((o for o in opinions if o.get("id") == opinion_id), None)
    if opinion is None:
        raise HTTPException(status_code=404, detail="Second opinion request not found.")
    if opinion.get("to_doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="This request is not addressed to you.")
    opinion["response"] = req.response
    opinion["status"] = "responded"
    opinion["responded_at"] = datetime.now(timezone.utc).isoformat()
    _save_second_opinions(opinions)

    appointments = _load_appointments()
    appt = appointments.get(opinion.get("appointment_id", ""))
    if appt and appt.get("second_opinion"):
        appt["second_opinion"]["status"] = "responded"
        appt["second_opinion"]["response"] = req.response
        _save_appointments(appointments)

    users = _load_users()
    from_doctor_user = next(
        (u for u in users.values() if u.get("doctor_id") == opinion.get("from_doctor_id")),
        None,
    )
    if from_doctor_user:
        _send_email_notification(
            to=from_doctor_user.get("email", ""),
            subject=f"Second Opinion Response — {opinion.get('patient_name', 'Patient')}",
            body=(
                f"Hi {opinion.get('from_doctor_name', 'Doctor')},\n\n"
                f"Dr. {doctor.get('name', '')} has responded to your second opinion request.\n\n"
                f"Patient: {opinion.get('patient_name', '')}\n"
                f"Response: {req.response}\n"
                f"\nPlease log in to the doctor portal to view the full response.\n"
            ),
        )

    return {"responded": True}
