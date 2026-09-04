"""/api/auth/* — moved from web/api.py as Task 4's first split-out router;
register/login/refresh/logout rewritten for Task 5 (real JWT auth).
User identity (users/patients/doctors/dependents) is now real relational
data (Phase 1 identity cutover) — `UserRepository` is the source of
truth. `_hash_password`/`_verify_password`/`_sessions`/session-history
helpers are still blob-adjacent module state in web/api.py that hasn't
been extracted yet (diagnostic sessions, not identity — out of scope for
the identity cutover); those are still imported lazily from web.api
inside each function that needs them, to avoid a circular import since
web.api is what includes this router.

Task 5 notes:
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
    """Issue a fresh access + refresh token pair for a user dict (as
    returned by web.api._compose_user_dict), persisting the refresh
    token's hash in the relational refresh_tokens table. The user row
    itself is guaranteed to already exist relationally (created directly
    by register, or already migrated) — no shadow-row upsert needed here
    since Phase 1's identity cutover."""
    user_id = uuid.UUID(str(user["id"]))
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
    from web.api import _compose_user_dict, _hash_password

    if UserRepository(db).get_by_email(req.email) is not None:
        raise HTTPException(status_code=409, detail="Email already registered.")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")
    dto = UserRepository(db).create_patient(
        name=req.name.strip(),
        email=req.email.lower().strip(),
        password_hash=_hash_password(req.password),
    )
    user = _compose_user_dict(db, dto)
    access_token, refresh_token = _issue_token_pair(db, user)
    log_audit_event(actor=user, action="register", resource_type="user", resource_id=user["id"], ip_address=_client_ip(request))
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": {"id": user["id"], "name": user["name"], "email": user["email"], "role": "patient"},
    }


@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, req: LoginRequest, db: Session = Depends(get_session)):
    from web.api import _compose_user_dict, _verify_password

    dto = UserRepository(db).get_by_email(req.email)
    if dto is None or not _verify_password(req.password, dto.password_hash):
        log_audit_event(
            actor=None, action="login_failed", resource_type="user",
            resource_id=req.email.lower().strip(), ip_address=_client_ip(request),
        )
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    matched = _compose_user_dict(db, dto)
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
    from web.api import _compose_user_dict

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

    dto = UserRepository(db).get_by_id(record.user_id)
    if dto is None:
        raise HTTPException(status_code=401, detail="User no longer exists.")
    user = _compose_user_dict(db, dto)

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
        _load_user_sessions,
    )

    # Backfill: pick up sessions that were started anonymously but later linked
    # to the user via appointment booking.
    uid = user["id"]
    existing_ids = {s.get("session_id") for s in _load_user_sessions(uid)}
    appointments = _load_appointments()
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
    # Re-read: cheap (single blob-collection lookup, not a full user scan),
    # so no need to track whether a backfill actually happened above.
    sessions = list(reversed(_load_user_sessions(uid)))
    return {"sessions": sessions}


@router.delete("/sessions/{session_id}")
async def delete_user_session(session_id: str, user: dict = Depends(get_current_user)):
    """Remove a session from the user's history and delete its log files."""
    from web.api import _repo_root, _load_user_sessions, _save_user_sessions, _sessions

    uid = user["id"]
    original = _load_user_sessions(uid)
    filtered = [s for s in original if s.get("session_id") != session_id]
    if len(filtered) == len(original):
        raise HTTPException(status_code=404, detail="Session not found.")
    _save_user_sessions(uid, filtered)
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
async def update_profile(
    req: ProfileUpdateRequest,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    uid = uuid.UUID(user["id"])
    fields = req.model_dump(exclude_none=True)
    name = fields.pop("name", None)
    if user.get("role") == "doctor":
        # Patient-only fields (mobile/age/etc.) are meaningless for a doctor
        # account and silently dropped here — same no-op behavior the blob
        # store had (extra dict keys nobody read), not a new restriction.
        UserRepository(db).update_doctor(uid, name=name)
    else:
        UserRepository(db).update_patient(uid, name=name, **fields)
    db.commit()
    return {"updated": True}


def _dependent_dict(dep) -> dict:
    return {
        "id": str(dep.id),
        "name": dep.name,
        "relationship": dep.relationship,
        "age": dep.age,
        "gender": dep.gender,
        "blood_type": dep.blood_type,
        "allergies": dep.allergies,
        "chronic_conditions": dep.chronic_conditions,
    }


@router.get("/dependents")
async def get_dependents(user: dict = Depends(get_current_user), db: Session = Depends(get_session)):
    deps = UserRepository(db).list_dependents(uuid.UUID(user["id"]))
    return {"dependents": [_dependent_dict(d) for d in deps]}


@router.post("/dependents")
async def add_dependent(
    req: DependentRequest,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    dto = UserRepository(db).add_dependent(uuid.UUID(user["id"]), **req.model_dump())
    db.commit()
    return {"saved": True, "dependent": _dependent_dict(dto)}


@router.patch("/dependents/{dep_id}")
async def update_dependent(
    dep_id: str,
    req: DependentRequest,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    try:
        dep_uuid = uuid.UUID(dep_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Dependent not found.")
    repo = UserRepository(db)
    # Ownership check: update_dependent takes a bare dependent_id with no
    # inherent scoping, so this must be verified against the caller's own
    # dependents here — same scoping the blob store had implicitly by only
    # ever searching within users[uid]["dependents"].
    owned = any(d.id == dep_uuid for d in repo.list_dependents(uuid.UUID(user["id"])))
    if not owned:
        raise HTTPException(status_code=404, detail="Dependent not found.")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    repo.update_dependent(dep_uuid, **fields)
    db.commit()
    return {"updated": True}


@router.delete("/dependents/{dep_id}")
async def delete_dependent(
    dep_id: str,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    try:
        dep_uuid = uuid.UUID(dep_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Dependent not found.")
    repo = UserRepository(db)
    owned = any(d.id == dep_uuid for d in repo.list_dependents(uuid.UUID(user["id"])))
    if not owned:
        raise HTTPException(status_code=404, detail="Dependent not found.")
    repo.delete_dependent(dep_uuid)
    db.commit()
    return {"deleted": True}
