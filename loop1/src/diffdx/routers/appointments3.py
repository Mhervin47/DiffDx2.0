"""Continuation of the appointments/doctor/patient domain — schedule
template application, no-show analytics, patient appointment history
timeline, pre-visit intake, prescription refill request/fulfill/pending
count, and direct booking. Task 4's tenth split-out router for this
domain. See routers/appointments.py's module docstring for the overall
approach.

Two routes here (fulfill_refill, get_pending_refills) use an inline
doctor-role check that's subtly different from _require_doctor /
require_role("doctor") — they collapse "not authenticated" and "wrong
role" into a single 403, rather than 401-then-403. Preserved exactly
rather than swapped for the dependency, since that would be an
observable behavior change (Task 4 promises zero of those).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from diffdx.dependencies import get_current_user, require_role
from diffdx.schemas.appointments import (
    DirectBookRequest,
    IntakeRequest,
    RefillRequest,
    ScheduleTemplateRequest,
)

router = APIRouter(tags=["appointments"])

# Feature 2 — Weekly Schedule Template
_WEEKDAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _slots_from_range(day_date, start: str, end: str) -> list[str]:
    """Generate 30-min ISO slots between start and end (HH:MM) for a date."""
    try:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
    except Exception:
        return []
    cur = day_date.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_dt = day_date.replace(hour=eh, minute=em, second=0, microsecond=0)
    out = []
    while cur < end_dt:
        out.append(cur.strftime("%Y-%m-%dT%H:%M"))
        cur += timedelta(minutes=30)
    return out


@router.post("/api/doctor/schedule-template/apply")
async def apply_schedule_template(req: ScheduleTemplateRequest, doctor_user: dict = Depends(require_role("doctor"))):
    """Generate ISO slots for the next N weeks from a weekly template and merge into available_slots."""
    from web.api import _load_doctors, _save_doctors

    doctor_id = doctor_user.get("doctor_id")
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == doctor_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doctor record not found.")

    weeks = max(1, min(int(req.weeks or 4), 12))
    today = datetime.now(timezone.utc).date()
    # Monday of the current week
    week_monday = today - timedelta(days=today.weekday())

    generated: set[str] = set()
    for w in range(weeks):
        for day_idx, day_key in enumerate(_WEEKDAY_ORDER):
            ranges = req.template.get(day_key) or []
            if not ranges:
                continue
            day_date = datetime.combine(
                week_monday + timedelta(weeks=w, days=day_idx),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            for r in ranges:
                for s in _slots_from_range(day_date, r.start, r.end):
                    # Skip slots that are already in the past
                    if datetime.fromisoformat(s).replace(tzinfo=timezone.utc) >= datetime.now(timezone.utc):
                        generated.add(s)

    merged = sorted(set(doc.get("available_slots", [])) | generated)
    doc["available_slots"] = merged
    _save_doctors(doctors)
    return {"generated": len(generated), "total_slots": len(merged)}


# Feature 3 — No-show Analytics
@router.get("/api/doctor/analytics")
async def get_doctor_analytics(doctor_user: dict = Depends(require_role("doctor"))):
    """Compute KPI / utilization / no-show analytics from the doctor's appointments."""
    from web.api import _load_appointments, _load_doctors

    doctor_id = doctor_user.get("doctor_id")
    appointments = _load_appointments()
    mine = [a for a in appointments.values() if a.get("doctor_id") == doctor_id]

    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == doctor_id), None)
    open_slots = doc.get("available_slots", []) if doc else []

    total_appts = len(mine)
    no_shows = [a for a in mine if a.get("status") == "no_show"]
    booked_count = total_appts
    open_count = len(open_slots)
    capacity = booked_count + open_count
    utilization_pct = round((booked_count / capacity) * 100) if capacity else 0
    noshow_rate = round((len(no_shows) / total_appts) * 100) if total_appts else 0
    total_patients = len({a.get("patient_name", "") for a in mine if a.get("patient_name")})

    # Weekly buckets — last 8 ISO weeks ending this week
    now = datetime.now(timezone.utc)
    week_keys = []
    for i in range(7, -1, -1):
        ref = now - timedelta(weeks=i)
        iso = ref.isocalendar()
        week_keys.append((iso[0], iso[1]))

    buckets = {wk: {"booked": 0, "noshow": 0} for wk in week_keys}
    for a in mine:
        slot = a.get("slot", "")
        if not slot:
            continue
        try:
            dt = datetime.fromisoformat(slot)
        except Exception:
            continue
        iso = dt.isocalendar()
        wk = (iso[0], iso[1])
        if wk in buckets:
            if a.get("status") == "no_show":
                buckets[wk]["noshow"] += 1
            else:
                buckets[wk]["booked"] += 1

    # Distribute open slots into their weeks for the "open" stack segment
    open_by_week: dict[tuple, int] = {}
    for s in open_slots:
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            continue
        iso = dt.isocalendar()
        wk = (iso[0], iso[1])
        if wk in buckets:
            open_by_week[wk] = open_by_week.get(wk, 0) + 1

    weekly = []
    for n, wk in enumerate(week_keys, start=1):
        weekly.append({
            "label": f"Wk {wk[1]}",
            "booked": buckets[wk]["booked"],
            "noshow": buckets[wk]["noshow"],
            "open": open_by_week.get(wk, 0),
        })

    # No-show patients table
    ns_map: dict[str, dict] = {}
    for a in mine:
        name = a.get("patient_name", "")
        if not name:
            continue
        entry = ns_map.setdefault(name, {"patient_name": name, "count": 0, "last_date": "", "total": 0})
        entry["total"] += 1
        if a.get("status") == "no_show":
            entry["count"] += 1
            slot = a.get("slot", "")
            if slot > entry["last_date"]:
                entry["last_date"] = slot
    noshows = [e for e in ns_map.values() if e["count"] > 0]
    noshows.sort(key=lambda e: e["count"], reverse=True)

    return {
        "kpis": {
            "total_appts": total_appts,
            "utilization_pct": utilization_pct,
            "noshow_rate": noshow_rate,
            "total_patients": total_patients,
        },
        "weekly": weekly,
        "noshows": noshows,
    }


# Feature 5 — Patient Appointment History Timeline
@router.get("/api/patient/history")
async def get_patient_history_timeline(user: dict = Depends(get_current_user)):
    """Return the patient's own appointments newest-first, with session_id where available."""
    from web.api import _load_appointments

    appointments = _load_appointments()
    mine = [a for a in appointments.values() if a.get("patient_user_id") == user["id"]]
    mine.sort(key=lambda a: a.get("slot", ""), reverse=True)
    out = []
    for a in mine:
        out.append({
            "appointment_id": a.get("appointment_id"),
            "session_id": a.get("session_id", ""),
            "slot": a.get("slot", ""),
            "doctor_name": a.get("doctor_name", ""),
            "specialty": a.get("specialty", ""),
            "primary_diagnosis": a.get("primary_diagnosis", ""),
            "status": a.get("status", "upcoming"),
            "prescriptions": a.get("prescriptions", []),
        })
    return {"history": out}


# Feature 6 — Pre-visit Symptom Intake
@router.post("/api/patient/appointments/{appt_id}/intake")
async def save_intake(appt_id: str, req: IntakeRequest, request: Request):
    from web.api import _save_appointments
    from diffdx.routers.appointments4 import _patient_appt_or_403

    appointments, appt = _patient_appt_or_403(appt_id, request)
    appt["intake"] = {
        **req.model_dump(),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_appointments(appointments)
    return {"saved": True}


# Feature 7 — Prescription Refill Request
@router.post("/api/patient/appointments/{appt_id}/refill")
async def request_refill(appt_id: str, req: RefillRequest, request: Request):
    from web.api import (
        _get_user_from_request,
        _load_doctors,
        _load_users,
        _save_appointments,
        _send_email_notification,
    )
    from diffdx.routers.appointments4 import _patient_appt_or_403

    appointments, appt = _patient_appt_or_403(appt_id, request)
    patient_user = _get_user_from_request(request)
    appt["refill_request"] = {
        "medications": req.medications,
        "note": req.note,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }
    _save_appointments(appointments)

    # Notify doctor by email
    doctor_id = appt.get("doctor_id", "")
    doctor_email = ""
    if doctor_id:
        doc = next((d for d in _load_doctors() if d.get("doctor_id") == doctor_id or d.get("id") == doctor_id), None)
        if doc:
            doctor_email = doc.get("email", "")
        if not doctor_email:
            # Fall back to users.json (doctor accounts)
            all_users = _load_users()
            for u in all_users.values():
                if u.get("doctor_id") == doctor_id:
                    doctor_email = u.get("email", "")
                    break
    patient_name = patient_user.get("name", "A patient") if patient_user else "A patient"
    drug_list = ", ".join(m.get("drug", m) if isinstance(m, dict) else str(m) for m in (req.medications or []))
    _send_email_notification(
        to=doctor_email,
        subject=f"Refill Request — {patient_name}",
        body=(
            f"Hi {appt.get('doctor_name', 'Doctor')},\n\n"
            f"{patient_name} has requested a prescription refill.\n"
            + (f"Medications: {drug_list}\n" if drug_list else "")
            + (f"Note: {req.note}\n" if req.note else "")
            + f"\nPlease log in to the doctor portal to review and approve.\n"
        ),
    )
    return {"queued": True}


@router.patch("/api/doctor/appointments/{appt_id}/refill")
async def fulfill_refill(appt_id: str, request: Request):
    """Doctor marks a refill request as given/fulfilled.

    IDOR fix (Task 5 audit): the original had no ownership check at all —
    any doctor account could fulfill any other doctor's patient's refill
    request, and the confirmation email sent to the patient would name the
    *requesting* doctor as having approved it (appt["refill_request"]
    ["fulfilled_by"] / the email body's doctor_name), which is both an
    authorization bug and a misattribution one. Added the same ownership
    check every other appt-scoped doctor route in this domain already has.
    """
    from web.api import _get_user_from_request, _load_appointments, _load_users, _save_appointments, _send_email_notification

    doctor_user = _get_user_from_request(request)
    if not doctor_user or doctor_user.get("role") != "doctor":
        raise HTTPException(status_code=403, detail="Doctor access required.")
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor_user.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    if not appt.get("refill_request"):
        raise HTTPException(status_code=404, detail="No refill request on this appointment.")
    appt["refill_request"]["status"] = "fulfilled"
    appt["refill_request"]["fulfilled_at"] = datetime.now(timezone.utc).isoformat()
    appt["refill_request"]["fulfilled_by"] = doctor_user.get("name", "")
    _save_appointments(appointments)

    # Notify patient by email
    patient_uid = appt.get("patient_user_id", "")
    if patient_uid:
        users = _load_users()
        patient = users.get(patient_uid, {})
        patient_email = patient.get("email", "")
        patient_name  = patient.get("name", "Patient")
        doctor_name   = doctor_user.get("name", "Your doctor")
        drug_list = ", ".join(
            m.get("drug", m) if isinstance(m, dict) else str(m)
            for m in (appt["refill_request"].get("medications") or [])
        )
        _send_email_notification(
            to=patient_email,
            subject="Your Prescription Refill Has Been Approved",
            body=(
                f"Hi {patient_name},\n\n"
                f"Good news! {doctor_name} has approved your prescription refill.\n"
                + (f"Medications: {drug_list}\n" if drug_list else "")
                + f"\nYou can collect your prescription from your pharmacy.\n"
                + f"If you have any questions, please message your doctor through the portal.\n"
            ),
        )
    return {"fulfilled": True}


@router.get("/api/doctor/pending-refills")
async def get_pending_refills(request: Request):
    """Return count of appointments with pending refill requests for the logged-in doctor."""
    from web.api import _get_user_from_request, _load_appointments

    doctor_user = _get_user_from_request(request)
    if not doctor_user or doctor_user.get("role") != "doctor":
        raise HTTPException(status_code=403, detail="Doctor access required.")
    appointments = _load_appointments()
    count = sum(
        1 for a in appointments.values()
        if a.get("doctor_id") == doctor_user.get("doctor_id")
        and (a.get("refill_request") or {}).get("status") == "pending"
    )
    return {"pending": count}


# Feature 8 — Doctor Search + Direct Booking
@router.post("/api/patient/book-direct")
async def book_direct(req: DirectBookRequest, user: dict = Depends(get_current_user)):
    from web.api import _load_blocked_dates, _load_doctors, _save_appointments, _save_doctors, _send_email_notification, _load_appointments

    doctors = _load_doctors()
    doctor = next((d for d in doctors if d["id"] == req.doctor_id), None)
    if doctor is None:
        raise HTTPException(status_code=404, detail="Doctor not found.")
    if req.slot not in doctor.get("available_slots", []):
        raise HTTPException(status_code=400, detail="Slot not available.")
    blocked_data = _load_blocked_dates()
    blocked_for_doc = {d["date"] for d in blocked_data.get(req.doctor_id, [])}
    if req.slot[:10] in blocked_for_doc:
        raise HTTPException(status_code=400, detail="This date is blocked by the doctor.")

    appt_id = str(uuid.uuid4())
    appt = {
        "appointment_id": appt_id,
        "session_id": "",
        "patient_user_id": user["id"],
        "patient_name": req.patient_name_override or user.get("name", "Anonymous Patient"),
        "doctor_id": doctor["id"],
        "doctor_name": doctor["name"],
        "specialty": doctor["specialty"],
        "slot": req.slot,
        "booked_at": datetime.now(timezone.utc).isoformat(),
        "primary_diagnosis": "",
        "urgency": "routine",
        "status": "upcoming",
        "is_direct_booking": True,
        "patient_note": req.note,
        "dependent_id": req.dependent_id,
        "booked_by_user_id": user["id"],
        "age": user.get("age"),
        "sex": user.get("sex"),
        "bmi": user.get("bmi"),
    }
    appointments = _load_appointments()
    appointments[appt_id] = appt
    _save_appointments(appointments)

    doctor["available_slots"] = [s for s in doctor["available_slots"] if s != req.slot]
    _save_doctors(doctors)

    # Email confirmation
    slot_fmt = req.slot.replace("T", " at ").replace(":00", "")
    _send_email_notification(
        to=user.get("email", ""),
        subject="Appointment Confirmed",
        body=f"Hi {user.get('name','')},\n\nYour appointment with {doctor['name']} ({doctor['specialty']}) is confirmed for {slot_fmt}.\n\nAppointment ID: {appt_id}",
    )

    return {"appointment_id": appt_id, "doctor_name": doctor["name"], "slot": req.slot}
