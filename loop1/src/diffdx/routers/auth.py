"""/api/auth/* — moved from web/api.py as Task 4's first split-out router;
register/login/refresh/logout rewritten for Task 5 (real JWT auth).

Every legacy blob-store helper this still calls (_load_users, _save_users,
_hash_password, etc.) is imported lazily from web.api inside each
function — that module-level state (the blob store helpers, _sessions,
_USER_CACHE) hasn't been extracted yet; doing so is real remaining work
for a later pass (see TASK4_SPLIT_ROUTERS.md). Importing here at call
time rather than module top avoids a circular import, since web.api is
what includes this router.

Task 5 notes:
- User identity itself is still the blob store (see
  diffdx.repositories.users.UserRepository.shadow_user's docstring for
  why a minimal relational shadow row gets upserted on every
  register/login anyway — refresh_tokens and audit_log_entries both have
  a real FK to users.id).
- Rate limiting (slowapi, 5/minute per IP) is on /register and /login
  only, per the spec — not /refresh or the rest, which aren't the
  credential-guessing surface these two are.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from diffdx.audit import log_audit_event
from diffdx.auth_tokens import (
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
    refresh_token_expiry,
)
from diffdx.db.engine import get_session
from diffdx.dependencies import get_current_user
from diffdx.rate_limit import limiter
from diffdx.repositories.users import RefreshTokenRepository, UserRepository
from diffdx.schemas.auth import (
    DependentRequest,
    LoginRequest,
    LogoutRequest,
    ProfileUpdateRequest,
    RefreshRequest,
    RegisterRequest,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _issue_token_pair(db: Session, user: dict) -> tuple[str, str]:
    """Issue a fresh access + refresh token pair for a (blob-store) user
    dict, persisting the refresh token's hash in the relational
    refresh_tokens table. Ensures the Task 5 shadow user row exists first
    (see UserRepository.shadow_user) so the FK is satisfiable."""
    user_id = uuid.UUID(str(user["id"]))
    UserRepository(db).shadow_user(
        id=user_id,
        name=user.get("name", ""),
        email=user.get("email", ""),
        password_hash=user.get("password_hash", ""),
        role=user.get("role", "patient"),
    )
    access_token = create_access_token(user["id"], user.get("role", "patient"))
    raw_refresh = generate_refresh_token()
    RefreshTokenRepository(db).create(
        user_id=user_id,
        token_hash=hash_refresh_token(raw_refresh),
        expires_at=refresh_token_expiry(),
    )
    db.commit()
    return access_token, raw_refresh


@router.post("/register")
@limiter.limit("5/minute")
async def register(request: Request, req: RegisterRequest, db: Session = Depends(get_session)):
    from web.api import _hash_password, _load_users, _save_users

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
        "role": "patient",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sessions": [],
    }
    users[user_id] = user
    _save_users(users)
    access_token, refresh_token = _issue_token_pair(db, user)
    log_audit_event(actor=user, action="register", resource_type="user", resource_id=user_id, ip_address=_client_ip(request))
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": {"id": user_id, "name": user["name"], "email": user["email"], "role": "patient"},
    }


@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, req: LoginRequest, db: Session = Depends(get_session)):
    from web.api import _load_users, _verify_password

    users = _load_users()
    matched = next(
        (u for u in users.values() if u["email"].lower() == req.email.lower()),
        None,
    )
    if not matched or not _verify_password(req.password, matched["password_hash"]):
        log_audit_event(
            actor=None, action="login_failed", resource_type="user",
            resource_id=req.email.lower().strip(), ip_address=_client_ip(request),
        )
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    access_token, refresh_token = _issue_token_pair(db, matched)
    log_audit_event(actor=matched, action="login", resource_type="user", resource_id=matched["id"], ip_address=_client_ip(request))
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": {
            "id": matched["id"],
            "name": matched["name"],
            "email": matched["email"],
            "role": matched.get("role", "patient"),
            "doctor_id": matched.get("doctor_id"),
            "specialty": matched.get("specialty"),
        },
    }


@router.post("/refresh")
async def refresh(req: RefreshRequest, db: Session = Depends(get_session)):
    """Exchange a valid, unexpired, unrevoked refresh token for a new
    access + refresh token pair. Rotates: the old refresh token is
    revoked in the same transaction a new one is issued, so a stolen-then-
    reused token is a one-shot — the legitimate client's next refresh
    attempt with the now-revoked token fails, which is a signal worth
    alerting on operationally (not implemented here — logged to the audit
    trail as `refresh_reuse_rejected`, which is enough to alert on later)."""
    from web.api import _load_users

    token_hash = hash_refresh_token(req.refresh_token)
    repo = RefreshTokenRepository(db)
    record = repo.get_by_hash(token_hash)
    now = datetime.now(timezone.utc)
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")
    if record.revoked_at is not None:
        log_audit_event(actor=None, action="refresh_reuse_rejected", resource_type="refresh_token", resource_id=str(record.id))
        raise HTTPException(status_code=401, detail="Refresh token has been revoked.")
    if record.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=401, detail="Refresh token has expired.")

    users = _load_users()
    user = users.get(str(record.user_id))
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists.")

    repo.revoke(record.id)
    access_token, new_refresh_token = _issue_token_pair(db, user)
    return {"access_token": access_token, "refresh_token": new_refresh_token}


@router.post("/logout")
async def logout(req: LogoutRequest, db: Session = Depends(get_session)):
    """Revoke a single refresh token (the one the client is holding).
    Always returns success — revoking an already-invalid/unknown token is
    not an error from the client's perspective, it just means they're
    logged out either way."""
    repo = RefreshTokenRepository(db)
    record = repo.get_by_hash(hash_refresh_token(req.refresh_token))
    if record is not None and record.revoked_at is None:
        repo.revoke(record.id)
        db.commit()
    return {"logged_out": True}


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
