"""/api/auth/* — moved from web/api.py as Task 4's first split-out router.

Logic is unchanged from the original monolith routes; only the location
and (where it simplifies without changing behavior) the auth check moved
to the get_current_user dependency. Every helper this still calls
(_load_users, _save_users, _hash_password, etc.) is imported lazily from
web.api inside each function — that module-level state (the blob store
helpers, _sessions, _TOKENS/_USER_CACHE) hasn't been extracted yet; doing
so is real remaining work for when the rest of the routers get split (see
TASK4_SPLIT_ROUTERS.md). Importing here at call time rather than module
top avoids a circular import, since web.api is what includes this router.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from diffdx.dependencies import get_current_user
from diffdx.schemas.auth import (
    DependentRequest,
    LoginRequest,
    ProfileUpdateRequest,
    RegisterRequest,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register")
async def register(req: RegisterRequest):
    from web.api import _hash_password, _issue_token, _load_users, _save_users

    users = _load_users()
    for u in users.values():
        if u["email"].lower() == req.email.lower():
            raise HTTPException(status_code=409, detail="Email already registered.")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")
    user_id = str(uuid.uuid4())
    user = {
        "id": user_id,
        "name": req.name.strip(),
        "email": req.email.lower().strip(),
        "password_hash": _hash_password(req.password),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sessions": [],
        "tokens": [],
    }
    users[user_id] = user
    _save_users(users)
    token = _issue_token(user_id)
    return {"token": token, "user": {"id": user_id, "name": user["name"], "email": user["email"], "role": "patient"}}


@router.post("/login")
async def login(req: LoginRequest):
    from web.api import _issue_token, _load_users, _verify_password

    users = _load_users()
    matched = next(
        (u for u in users.values() if u["email"].lower() == req.email.lower()),
        None,
    )
    if not matched or not _verify_password(req.password, matched["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = _issue_token(matched["id"])
    return {
        "token": token,
        "user": {
            "id": matched["id"],
            "name": matched["name"],
            "email": matched["email"],
            "role": matched.get("role", "patient"),
            "doctor_id": matched.get("doctor_id"),
            "specialty": matched.get("specialty"),
        },
    }


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "role": user.get("role", "patient"),
        "doctor_id": user.get("doctor_id"),
        "specialty": user.get("specialty"),
    }


@router.get("/sessions")
async def get_user_sessions(user: dict = Depends(get_current_user)):
    from web.api import (
        _add_session_to_user,
        _load_appointments,
        _load_session_report_from_db,
        _load_users,
    )

    # Backfill: pick up sessions that were started anonymously but later linked
    # to the user via appointment booking.
    users = _load_users()
    uid = user["id"]
    existing_ids = {s.get("session_id") for s in users[uid].get("sessions", [])}
    appointments = _load_appointments()
    added = False
    for appt in appointments.values():
        sid = appt.get("session_id", "")
        if not sid or appt.get("patient_user_id") != uid or sid in existing_ids:
            continue
        report = _load_session_report_from_db(sid)
        chief_complaint = ""
        started_at = appt.get("booked_at", datetime.now(timezone.utc).isoformat())
        final_dx = None
        ended_at = None
        status = "active"
        if report:
            chief_complaint = report.get("patient", {}).get("chief_complaint", "")
            started_at = report.get("started_at", started_at)
            final_dx = (report.get("primary_diagnosis")
                        or (report.get("final_differential") or [{}])[0].get("dx"))
            ended_at = report.get("ended_at")
            status = "complete" if report.get("termination_reason") else "active"
        _add_session_to_user(uid, {
            "session_id": sid,
            "chief_complaint": chief_complaint,
            "started_at": started_at,
            "status": status,
            "primary_diagnosis": final_dx,
            "ended_at": ended_at,
        })
        existing_ids.add(sid)
        added = True
    if added:
        # Re-read the updated user record
        users = _load_users()
    sessions = list(reversed(users[uid].get("sessions", [])))
    return {"sessions": sessions}


@router.delete("/sessions/{session_id}")
async def delete_user_session(session_id: str, user: dict = Depends(get_current_user)):
    """Remove a session from the user's history and delete its log files."""
    from web.api import _repo_root, _save_users, _sessions, _load_users

    users = _load_users()
    uid = user["id"]
    original = users[uid].get("sessions", [])
    filtered = [s for s in original if s.get("session_id") != session_id]
    if len(filtered) == len(original):
        raise HTTPException(status_code=404, detail="Session not found.")
    users[uid]["sessions"] = filtered
    _save_users(users)
    # Remove from live session store
    _sessions.pop(session_id, None)
    # Delete log files (best-effort — ignore if missing)
    for pattern in [
        _repo_root / "logs" / "final_records" / f"final_{session_id}.json",
        _repo_root / "logs" / "sessions" / f"session_{session_id}.jsonl",
    ]:
        try:
            pattern.unlink(missing_ok=True)
        except Exception:
            pass
    return {"deleted": True}


@router.get("/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "mobile": user.get("mobile", ""),
        "age": user.get("age"),
        "blood_type": user.get("blood_type", ""),
        "gender": user.get("gender", ""),
        "address": user.get("address", ""),
        "emergency_contact_name": user.get("emergency_contact_name", ""),
        "emergency_contact_phone": user.get("emergency_contact_phone", ""),
        "allergies": user.get("allergies", ""),
        "chronic_conditions": user.get("chronic_conditions", ""),
        "created_at": user.get("created_at", ""),
        "profile_complete": bool(user.get("mobile") and user.get("age") and user.get("blood_type")),
    }


@router.patch("/profile")
async def update_profile(req: ProfileUpdateRequest, user: dict = Depends(get_current_user)):
    from web.api import _load_users, _save_users

    users = _load_users()
    uid = user["id"]
    fields = req.model_dump(exclude_none=True)
    for k, v in fields.items():
        users[uid][k] = v
    _save_users(users)
    return {"updated": True}


@router.get("/dependents")
async def get_dependents(user: dict = Depends(get_current_user)):
    from web.api import _load_users

    users = _load_users()
    return {"dependents": users[user["id"]].get("dependents", [])}


@router.post("/dependents")
async def add_dependent(req: DependentRequest, user: dict = Depends(get_current_user)):
    from web.api import _load_users, _save_users

    users = _load_users()
    dep = req.model_dump()
    dep["id"] = str(uuid.uuid4())
    users[user["id"]].setdefault("dependents", []).append(dep)
    _save_users(users)
    return {"saved": True, "dependent": dep}


@router.patch("/dependents/{dep_id}")
async def update_dependent(dep_id: str, req: DependentRequest, user: dict = Depends(get_current_user)):
    from web.api import _load_users, _save_users

    users = _load_users()
    deps = users[user["id"]].get("dependents", [])
    for d in deps:
        if d.get("id") == dep_id:
            d.update({k: v for k, v in req.model_dump().items() if v is not None})
            _save_users(users)
            return {"updated": True}
    raise HTTPException(status_code=404, detail="Dependent not found.")


@router.delete("/dependents/{dep_id}")
async def delete_dependent(dep_id: str, user: dict = Depends(get_current_user)):
    from web.api import _load_users, _save_users

    users = _load_users()
    deps = users[user["id"]].get("dependents", [])
    new_deps = [d for d in deps if d.get("id") != dep_id]
    if len(new_deps) == len(deps):
        raise HTTPException(status_code=404, detail="Dependent not found.")
    users[user["id"]]["dependents"] = new_deps
    _save_users(users)
    return {"deleted": True}
