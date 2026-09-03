"""Bare /api/doctors/* — the read-only doctor directory, not the big
appointment-mutation-heavy /api/doctor/* domain (33 routes, still in
web/api.py, deliberately saved for last — see TASK4_SPLIT_ROUTERS.md §2).

Task 4's fourth split-out router. Logic unchanged from the original.

_require_doctor(request) -> Depends(require_role("doctor")) is a genuine
behavioral match here, verified by reading _require_doctor first (not
assumed): both do get_current_user, then 403 if role != "doctor", return
the user dict. Confirmed equivalent before using it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from diffdx.dependencies import require_role

router = APIRouter(prefix="/api/doctors", tags=["doctors"])


@router.get("")
async def list_all_doctors():
    """Return all doctors (name, specialty, hospital, rating, avatar_initials, available_slots) for patient search."""
    from web.api import _load_blocked_dates, _load_doctors

    doctors = _load_doctors()
    blocked_data = _load_blocked_dates()
    now = datetime.now(timezone.utc).isoformat()

    def future_slots(doc_id, slots):
        blocked_dates = {d["date"] for d in blocked_data.get(doc_id, [])}
        out = []
        for s in slots:
            try:
                if s >= now[:16] and s[:10] not in blocked_dates:
                    out.append(s)
            except Exception:
                pass
        return out

    return {"doctors": [
        {
            "id": d["id"],
            "name": d["name"],
            "specialty": d["specialty"],
            "hospital": d["hospital"],
            "rating": d["rating"],
            "avatar_initials": d.get("avatar_initials", d["name"][:2].upper()),
            "available_slots": future_slots(d.get("id", ""), d.get("available_slots", [])),
        }
        for d in doctors
    ]}


@router.get("/{doctor_id}/slots")
async def get_doctor_slots(doctor_id: str, doctor: dict = Depends(require_role("doctor"))):
    """Return available slots for a doctor (for rescheduling)."""
    from web.api import _load_doctors

    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == doctor_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doctor not found.")
    return {"slots": doc.get("available_slots", [])}
