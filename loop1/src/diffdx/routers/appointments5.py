"""Penultimate bucket of the appointments/doctor/patient domain — symptom
history, appointment waitlist, doctor blocked-dates, patient tagging, and
prescription renewal reminders. Second-opinion requests moved to
appointments6.py to keep this file under 400 lines. See
routers/appointments.py's module docstring for the overall approach.

_load_waitlist/_save_waitlist/_load_blocked_dates/_save_blocked_dates
stay in web.api and are lazy-imported instead — each is already
referenced the same way from earlier-extracted router files
(appointments.py, appointments3.py, appointments4.py, doctors.py,
sessions.py), so moving their definitions here would just reproduce the
exact "shared helper stranded behind an extraction" bug this session
already found and fixed twice (see the part 4 commit).

_parse_duration_days is moved here even though nothing in the codebase
calls it (verified via grep — genuinely pre-existing dead code, not
introduced by this split). Left in place rather than removed, since
Task 4 promises zero behavior change, dead code included.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from diffdx.db.engine import get_session
from diffdx.repositories.scheduling import BlockedDateRepository, WaitlistRepository
from diffdx.repositories.users import UserRepository
from diffdx.schemas.appointments import (
    BlockDateRequest,
    TagsRequest,
    WaitlistRequest,
)

router = APIRouter(tags=["appointments"])
_log = logging.getLogger(__name__)


def _parse_duration_days(duration_str: str) -> int | None:
    """Parse a duration string like '7 days', '2 weeks', '1 month' into days."""
    if not duration_str:
        return None
    m = re.search(r'(\d+)\s*(day|week|month)', duration_str.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if unit == "day":
        return n
    if unit == "week":
        return n * 7
    if unit == "month":
        return n * 30
    return None


# ---------------------------------------------------------------------------
# Feature: Symptom history across sessions (patient)
# ---------------------------------------------------------------------------

@router.get("/api/patient/symptom-history")
async def get_symptom_history(request: Request):
    """Return aggregated symptoms from all past sessions for the authenticated patient."""
    from web.api import (
        _get_user_from_request,
        _load_appointments,
        _load_report_from_disk,
        _repo_root,
    )
    import json

    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    appointments = _load_appointments()
    entries = []
    for appt in appointments.values():
        if appt.get("patient_user_id") != user["id"]:
            continue
        if appt.get("status") not in ("seen", "upcoming"):
            continue
        session_id = appt.get("session_id")
        symptoms = []
        if session_id:
            jsonl_path = _repo_root / "logs" / f"session_{session_id}.jsonl"
            if jsonl_path.exists():
                try:
                    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        ev = json.loads(line)
                        profile = ev.get("patient_profile", {})
                        for s in profile.get("symptoms", []):
                            name = s.get("name", "")
                            if name and name not in symptoms:
                                symptoms.append(name)
                except Exception:
                    pass
        report = _load_report_from_disk(session_id) if session_id else None
        if report:
            for s in report.get("final_profile", {}).get("symptoms", []):
                name = s.get("name", "")
                if name and name not in symptoms:
                    symptoms.append(name)
        if symptoms or appt.get("primary_diagnosis"):
            entries.append({
                "appointment_id": appt["appointment_id"],
                "slot": appt.get("slot", ""),
                "doctor_name": appt.get("doctor_name", ""),
                "primary_diagnosis": appt.get("primary_diagnosis", ""),
                "symptoms": symptoms,
                "status": appt.get("status", ""),
            })
    entries.sort(key=lambda e: e["slot"], reverse=True)
    return {"history": entries}


# ---------------------------------------------------------------------------
# Feature: Appointment Waitlist
# ---------------------------------------------------------------------------

@router.post("/api/patient/waitlist")
async def join_waitlist(req: WaitlistRequest, request: Request, db: Session = Depends(get_session)):
    from web.api import _get_user_from_request, _load_waitlist, _save_waitlist

    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    waitlist = _load_waitlist()
    existing = next(
        (e for e in waitlist
         if e.get("patient_user_id") == user["id"]
         and e.get("doctor_id") == req.doctor_id
         and e.get("status") == "waiting"),
        None,
    )
    if existing:
        raise HTTPException(status_code=409, detail="Already on the waitlist for this doctor.")
    entry_id = str(uuid.uuid4())
    entry = {
        "id": entry_id,
        "patient_user_id": user["id"],
        "patient_name": user.get("name", ""),
        "doctor_id": req.doctor_id,
        "doctor_name": req.doctor_name,
        "specialty": req.specialty,
        "note": req.note,
        "joined_at": datetime.now(timezone.utc).isoformat(),
        "status": "waiting",
    }
    waitlist.append(entry)
    _save_waitlist(waitlist)

    try:
        doctor_dto = UserRepository(db).get_by_doctor_id(req.doctor_id)
        if doctor_dto is not None:
            WaitlistRepository(db).join(uuid.UUID(user["id"]), doctor_dto.id, note=req.note or None, id=uuid.UUID(entry_id))
            db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of waitlist join failed for entry %s", entry_id, exc_info=True)

    return {"joined": True, "entry": entry}


@router.get("/api/patient/waitlist")
async def get_patient_waitlist(request: Request):
    from web.api import _get_user_from_request, _load_waitlist

    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    waitlist = _load_waitlist()
    mine = [e for e in waitlist if e.get("patient_user_id") == user["id"]]
    return {"waitlist": mine}


@router.delete("/api/patient/waitlist/{entry_id}")
async def leave_waitlist(entry_id: str, request: Request, db: Session = Depends(get_session)):
    from web.api import _get_user_from_request, _load_waitlist, _save_waitlist

    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    waitlist = _load_waitlist()
    new_list = [e for e in waitlist if not (e.get("id") == entry_id and e.get("patient_user_id") == user["id"])]
    if len(new_list) == len(waitlist):
        raise HTTPException(status_code=404, detail="Waitlist entry not found.")
    _save_waitlist(new_list)

    try:
        entry_uuid = uuid.UUID(entry_id)
        WaitlistRepository(db).leave(entry_uuid)
        db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of waitlist leave failed for entry %s", entry_id, exc_info=True)

    return {"removed": True}


@router.get("/api/doctor/waitlist")
async def get_doctor_waitlist(request: Request):
    from web.api import _load_waitlist, _require_doctor

    doctor = _require_doctor(request)
    doctor_id = doctor.get("doctor_id")
    waitlist = _load_waitlist()
    mine = [e for e in waitlist if e.get("doctor_id") == doctor_id and e.get("status") == "waiting"]
    mine.sort(key=lambda e: e.get("joined_at", ""))
    return {"waitlist": mine, "count": len(mine)}


# ---------------------------------------------------------------------------
# Feature: Block Specific Dates
# ---------------------------------------------------------------------------

@router.post("/api/doctor/blocked-dates")
async def block_date(req: BlockDateRequest, request: Request, db: Session = Depends(get_session)):
    from web.api import _load_blocked_dates, _require_doctor, _save_blocked_dates

    doctor = _require_doctor(request)
    doctor_id = doctor.get("doctor_id")
    data = _load_blocked_dates()
    if doctor_id not in data:
        data[doctor_id] = []
    data[doctor_id] = [d for d in data[doctor_id] if d.get("date") != req.date]
    data[doctor_id].append({"date": req.date, "reason": req.reason})
    data[doctor_id].sort(key=lambda d: d["date"])
    _save_blocked_dates(data)

    try:
        doctor_dto = UserRepository(db).get_by_doctor_id(doctor_id)
        if doctor_dto is not None:
            # Blob logic removes any existing entry for this date then
            # appends the new one (re-blocking updates the reason) — mirror
            # that exactly, since block() alone would violate the unique
            # (doctor_id, date) constraint on a date already blocked.
            BlockedDateRepository(db).unblock(doctor_dto.id, req.date)
            BlockedDateRepository(db).block(doctor_dto.id, req.date, reason=req.reason or None)
            db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of blocked date failed for doctor %s date %s", doctor_id, req.date, exc_info=True)

    return {"blocked": True}


@router.get("/api/doctor/blocked-dates")
async def get_blocked_dates(request: Request):
    from web.api import _load_blocked_dates, _require_doctor

    doctor = _require_doctor(request)
    doctor_id = doctor.get("doctor_id")
    data = _load_blocked_dates()
    return {"blocked_dates": data.get(doctor_id, [])}


@router.delete("/api/doctor/blocked-dates/{date}")
async def unblock_date(date: str, request: Request, db: Session = Depends(get_session)):
    from web.api import _load_blocked_dates, _require_doctor, _save_blocked_dates

    doctor = _require_doctor(request)
    doctor_id = doctor.get("doctor_id")
    data = _load_blocked_dates()
    if doctor_id not in data:
        raise HTTPException(status_code=404, detail="No blocked dates found.")
    new_list = [d for d in data[doctor_id] if d.get("date") != date]
    if len(new_list) == len(data.get(doctor_id, [])):
        raise HTTPException(status_code=404, detail="Date not found in blocked list.")
    data[doctor_id] = new_list
    _save_blocked_dates(data)

    try:
        doctor_dto = UserRepository(db).get_by_doctor_id(doctor_id)
        if doctor_dto is not None:
            BlockedDateRepository(db).unblock(doctor_dto.id, date)
            db.commit()
    except Exception:
        db.rollback()
        _log.warning("Dual-write of unblock date failed for doctor %s date %s", doctor_id, date, exc_info=True)

    return {"unblocked": True}


# ---------------------------------------------------------------------------
# Feature: Patient Tagging
# ---------------------------------------------------------------------------

@router.patch("/api/doctor/appointments/{appt_id}/tags")
async def update_patient_tags(appt_id: str, req: TagsRequest, request: Request):
    from web.api import _load_appointments, _require_doctor, _save_appointments

    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["patient_tags"] = req.tags
    appt["tags_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)
    return {"saved": True, "tags": req.tags}


# ---------------------------------------------------------------------------
# Feature: Prescription Renewal Reminders
# ---------------------------------------------------------------------------

@router.get("/api/doctor/renewal-reminders")
async def get_renewal_reminders(request: Request):
    """Return appointments where the patient has submitted a pending refill request."""
    from web.api import _load_appointments, _require_doctor

    doctor = _require_doctor(request)
    doctor_id = doctor.get("doctor_id")
    appointments = _load_appointments()
    results = []
    for appt in appointments.values():
        if appt.get("doctor_id") != doctor_id:
            continue
        refill = appt.get("refill_request")
        if not refill or refill.get("status") == "fulfilled":
            continue
        meds = refill.get("medications", [])
        med_names = [m if isinstance(m, str) else m.get("drug", "") for m in meds]
        results.append({
            "appointment_id": appt.get("appointment_id"),
            "patient_name": appt.get("patient_name", ""),
            "medications": med_names,
            "note": refill.get("note", ""),
            "requested_at": refill.get("requested_at", appt.get("slot", "")),
            "slot": appt.get("slot", ""),
        })
    results.sort(key=lambda r: r["requested_at"], reverse=True)
    return {"reminders": results, "count": len(results)}
