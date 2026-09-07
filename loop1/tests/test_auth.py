"""Task 5: JWT auth, refresh token rotation, RBAC, rate limiting.

Isolation strategy (this is the first test file in the suite that boots
the real FastAPI app — worth explaining):

- The relational DB (refresh_tokens, audit_log_entries, and — since the
  identity cutover — users/patients/doctors/dependents too) is redirected
  to a private temp SQLite file for the whole module, via monkeypatching
  diffdx.db.engine.get_engine/get_sessionmaker rather than relying on
  DATABASE_URL + import order (fragile — other test modules may or may
  not have already triggered the engine singleton). Every place that
  needs a session (routers/auth.py's Depends(get_session),
  diffdx.audit.log_audit_event, and now web.api's identity helpers —
  _load_users/_user_from_access_token, which all
  resolve get_sessionmaker() by name at call time) picks up this
  monkeypatch transparently, so user creation in this module never
  touches the live dev DB at web/data/diffdx.db — the collision-avoidance
  workaround this docstring used to describe (uuid4-suffixed emails,
  manual cleanup) is no longer needed for user data specifically. Blob
  collections that stayed out of scope for the identity cutover
  (appointments, the doctor directory, etc.) still use the real
  web/data/diffdx.db, so tests touching those still need the
  uuid4-suffixed/cleanup pattern (see _make_doctor's appointment
  fixture usage below, and test_concurrency.py).
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from diffdx.db import models  # noqa: F401 — registers all models on Base
from diffdx.db.base import Base
import diffdx.db.engine as db_engine


@pytest.fixture(scope="module")
def test_engine(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("auth_test") / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def test_sessionmaker(test_engine):
    return sessionmaker(bind=test_engine, expire_on_commit=False)


@pytest.fixture(scope="module", autouse=True)
def _redirect_db(test_engine, test_sessionmaker):
    """Monkeypatch the relational DB layer to the isolated test DB for
    every test in this module. Module-scoped (not the function-scoped
    `monkeypatch` fixture, which can't be used at module scope) — applied
    once, reverted once, since every test in this file wants the same
    isolated DB."""
    orig_get_engine = db_engine.get_engine
    orig_get_sessionmaker = db_engine.get_sessionmaker
    db_engine.get_sessionmaker = lambda: test_sessionmaker
    db_engine.get_engine = lambda: test_engine
    yield
    db_engine.get_engine = orig_get_engine
    db_engine.get_sessionmaker = orig_get_sessionmaker


@pytest.fixture(scope="module")
def client():
    from web.api import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Every test in this module shares one client/IP, so the 5/minute
    limit on /api/auth/register and /api/auth/login (shared across all
    tests, not just the dedicated rate-limit test below) would otherwise
    trip partway through the module from accumulated calls."""
    from diffdx.rate_limit import limiter
    limiter.reset()
    yield


def _unique_email() -> str:
    return f"auth.test.{uuid.uuid4()}@example.com"


@pytest.fixture(scope="module", autouse=True)
def _capture_otp_emails():
    """Register no longer emails a real inbox in tests — it calls
    diffdx.otp.send_otp_email, imported by name into diffdx.routers.auth,
    so that's the reference to patch. Captures {email: code} instead of
    actually sending, so tests can complete the verify-otp step without
    needing a real RESEND_API_KEY/SMTP_HOST configured."""
    import diffdx.routers.auth as auth_module

    captured: dict[str, str] = {}

    def _fake_send(to_email: str, name: str, code: str) -> bool:
        captured[to_email.lower()] = code
        return True

    orig = auth_module.send_otp_email
    auth_module.send_otp_email = _fake_send
    yield captured
    auth_module.send_otp_email = orig


def _register_and_verify(client, otp_emails, name, email, password):
    """Full register -> verify-otp round trip, returning verify-otp's
    response (the one that actually carries tokens now — register itself
    only confirms an OTP was sent)."""
    reg = client.post("/api/auth/register", json={"name": name, "email": email, "password": password})
    assert reg.status_code == 200, reg.text
    reg_data = reg.json()
    assert reg_data["verification_required"] is True
    assert reg_data["email"] == email.lower()

    code = otp_emails[email.lower()]
    return client.post("/api/auth/verify-otp", json={"email": email, "code": code})


# ---------------------------------------------------------------------------
# Register / login issue a JWT access + refresh pair
# ---------------------------------------------------------------------------

def test_register_does_not_issue_tokens_until_verified(client, _capture_otp_emails):
    email = _unique_email()
    res = client.post("/api/auth/register", json={"name": "Auth Test", "email": email, "password": "testpass123"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data == {"verification_required": True, "email": email.lower()}

    # Not verified yet — login must be refused, not silently allowed.
    login = client.post("/api/auth/login", json={"email": email, "password": "testpass123"})
    assert login.status_code == 403
    assert login.json()["detail"]["verification_required"] is True


def test_verify_otp_issues_access_and_refresh_tokens(client, _capture_otp_emails):
    email = _unique_email()
    res = _register_and_verify(client, _capture_otp_emails, "Auth Test", email, "testpass123")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["access_token"] and data["refresh_token"]
    assert data["access_token"] != data["refresh_token"]
    assert data["user"]["email"] == email.lower()

    # The access token is a real JWT carrying sub/role/exp/iat/jti — not an
    # opaque lookup key. Decoding it here (with no server-side state at
    # all) is exactly what proves a restart doesn't invalidate sessions:
    # verification only needs the signing key, never an in-memory table.
    from diffdx.config import settings
    claims = jwt.decode(data["access_token"], settings.resolved_secret_key, algorithms=["HS256"])
    assert claims["sub"] == data["user"]["id"]
    assert claims["role"] == "patient"
    assert {"exp", "iat", "jti"} <= claims.keys()


def test_verify_otp_wrong_code_rejected_and_decrements_attempts(client, _capture_otp_emails):
    email = _unique_email()
    client.post("/api/auth/register", json={"name": "Auth Test", "email": email, "password": "testpass123"})
    res = client.post("/api/auth/verify-otp", json={"email": email, "code": "000000"})
    assert res.status_code == 400
    # The real code still works afterward — one bad guess doesn't burn the code itself.
    code = _capture_otp_emails[email.lower()]
    res2 = client.post("/api/auth/verify-otp", json={"email": email, "code": code})
    assert res2.status_code == 200, res2.text


def test_resend_otp_issues_a_working_new_code(client, _capture_otp_emails):
    email = _unique_email()
    client.post("/api/auth/register", json={"name": "Auth Test", "email": email, "password": "testpass123"})
    client.post("/api/auth/resend-otp", json={"email": email})
    code = _capture_otp_emails[email.lower()]
    res = client.post("/api/auth/verify-otp", json={"email": email, "code": code})
    assert res.status_code == 200, res.text


def test_login_issues_fresh_token_pair(client, _capture_otp_emails):
    email = _unique_email()
    _register_and_verify(client, _capture_otp_emails, "Auth Test", email, "testpass123")
    res = client.post("/api/auth/login", json={"email": email, "password": "testpass123"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["access_token"] and data["refresh_token"]


def test_login_wrong_password_rejected(client, _capture_otp_emails):
    email = _unique_email()
    _register_and_verify(client, _capture_otp_emails, "Auth Test", email, "testpass123")
    res = client.post("/api/auth/login", json={"email": email, "password": "wrongpassword"})
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Restarting the server does not log users out (statelessness)
# ---------------------------------------------------------------------------

def test_access_token_verifies_with_no_server_state(client, _capture_otp_emails):
    """Simulates "restart" by decoding the token in total isolation from
    anything the request that issued it left behind — the in-memory
    _TOKENS dict this replaced is gone; there is no per-process state left
    to lose on restart."""
    email = _unique_email()
    res = _register_and_verify(client, _capture_otp_emails, "Auth Test", email, "testpass123")
    access_token = res.json()["access_token"]

    from diffdx.auth_tokens import decode_access_token
    claims = decode_access_token(access_token)
    assert claims["sub"] == res.json()["user"]["id"]

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me.status_code == 200


# ---------------------------------------------------------------------------
# Expired access token + valid refresh → transparent renewal
# ---------------------------------------------------------------------------

def test_expired_access_token_is_rejected(client, monkeypatch):
    from diffdx.config import settings
    from diffdx.auth_tokens import ALGORITHM

    # Craft a token that's already expired, signed with the real key —
    # this is what an access token looks like 15+ minutes after issuance.
    now = datetime.now(timezone.utc)
    expired_claims = {
        "sub": str(uuid.uuid4()), "role": "patient",
        "iat": now - timedelta(minutes=30), "exp": now - timedelta(minutes=15),
        "jti": str(uuid.uuid4()),
    }
    expired_token = jwt.encode(expired_claims, settings.resolved_secret_key, algorithm=ALGORITHM)

    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert res.status_code == 401


def test_refresh_with_valid_token_issues_new_working_pair(client, _capture_otp_emails):
    email = _unique_email()
    reg = _register_and_verify(client, _capture_otp_emails, "Auth Test", email, "testpass123")
    old_refresh = reg.json()["refresh_token"]

    res = client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["access_token"] and data["refresh_token"]
    assert data["refresh_token"] != old_refresh  # rotated, not reused

    # The new access token actually works.
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"})
    assert me.status_code == 200


# ---------------------------------------------------------------------------
# Revoked refresh token → 401, and cannot be reused
# ---------------------------------------------------------------------------

def test_refresh_token_rotation_rejects_reuse(client, _capture_otp_emails):
    """Using a refresh token revokes it (rotation) — using it again (e.g.
    a stolen, already-used token replayed by an attacker) must 401, not
    silently succeed a second time."""
    email = _unique_email()
    reg = _register_and_verify(client, _capture_otp_emails, "Auth Test", email, "testpass123")
    refresh_token = reg.json()["refresh_token"]

    first = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert first.status_code == 200

    second = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert second.status_code == 401


def test_logout_revokes_refresh_token(client, _capture_otp_emails):
    email = _unique_email()
    reg = _register_and_verify(client, _capture_otp_emails, "Auth Test", email, "testpass123")
    refresh_token = reg.json()["refresh_token"]

    logout = client.post("/api/auth/logout", json={"refresh_token": refresh_token})
    assert logout.status_code == 200

    res = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert res.status_code == 401


def test_refresh_with_unknown_token_is_401(client):
    res = client.post("/api/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# RBAC: patient token on any /api/doctor/* route → 403
# ---------------------------------------------------------------------------

def test_patient_token_on_doctor_route_is_403(client, _capture_otp_emails):
    email = _unique_email()
    reg = _register_and_verify(client, _capture_otp_emails, "Auth Test", email, "testpass123")
    access_token = reg.json()["access_token"]

    res = client.get("/api/doctor/appointments", headers={"Authorization": f"Bearer {access_token}"})
    assert res.status_code == 403


def test_no_token_on_doctor_route_is_401(client):
    res = client.get("/api/doctor/appointments")
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# IDOR fix: Doctor A cannot read Doctor B's appointment detail
# ---------------------------------------------------------------------------

def _make_doctor(name: str, doctor_id: str) -> tuple[dict, str]:
    """Doctors are normally only created via scripts/seed_doctors.py's
    fixed doctor list — registration only creates patients. Created directly via
    UserRepository here (same approach test_concurrency.py uses) rather
    than depending on which seeded doctor_ids happen to exist. Since the
    identity cutover, this goes through the module's monkeypatched
    relational test_engine like everything else in this file — no more
    blob-store/live-dev-DB collision risk for user data."""
    from web.api import _compose_user_dict, _hash_password
    from diffdx.routers.auth import _issue_token_pair
    from diffdx.db.engine import get_sessionmaker
    from diffdx.repositories.users import UserRepository

    with get_sessionmaker()() as db:
        dto = UserRepository(db).create_doctor(
            name=name,
            email=f"{doctor_id}.{uuid.uuid4()}@example.com",
            password_hash=_hash_password("Doctor123!"),
            doctor_id=doctor_id,
            specialty="General",
        )
        db.commit()
        user = _compose_user_dict(db, dto)
        access_token, _ = _issue_token_pair(db, user)
    return user, access_token


def test_doctor_a_cannot_read_doctor_b_appointment_detail(client):
    from web.api import _load_appointments, _save_appointments

    doctor_a, token_a = _make_doctor("Dr. A", f"test_dr_a_{uuid.uuid4().hex[:8]}")
    doctor_b, token_b = _make_doctor("Dr. B", f"test_dr_b_{uuid.uuid4().hex[:8]}")

    appt_id = str(uuid.uuid4())
    appointments = _load_appointments()
    appointments[appt_id] = {
        "appointment_id": appt_id, "session_id": "",
        "patient_user_id": str(uuid.uuid4()), "patient_name": "Test Patient",
        "doctor_id": doctor_a["doctor_id"], "doctor_name": doctor_a["name"],
        "specialty": "General", "slot": "2099-01-01T10:00",
        "status": "upcoming",
    }
    _save_appointments(appointments)
    try:
        owner_res = client.get(f"/api/doctor/appointments/{appt_id}", headers={"Authorization": f"Bearer {token_a}"})
        assert owner_res.status_code == 200, owner_res.text

        other_res = client.get(f"/api/doctor/appointments/{appt_id}", headers={"Authorization": f"Bearer {token_b}"})
        assert other_res.status_code == 403
    finally:
        appointments = _load_appointments()
        appointments.pop(appt_id, None)
        _save_appointments(appointments)


def test_doctor_patient_history_scoped_to_own_appointments(client):
    """IDOR fix: get_patient_history used to return ANY patient's history
    to ANY doctor by name, with no ownership check at all."""
    from web.api import _load_appointments, _save_appointments

    doctor_a, token_a = _make_doctor("Dr. A", f"test_dr_a_{uuid.uuid4().hex[:8]}")
    doctor_b, token_b = _make_doctor("Dr. B", f"test_dr_b_{uuid.uuid4().hex[:8]}")
    patient_name = f"History Test Patient {uuid.uuid4().hex[:8]}"

    appt_id = str(uuid.uuid4())
    appointments = _load_appointments()
    appointments[appt_id] = {
        "appointment_id": appt_id, "patient_name": patient_name,
        "doctor_id": doctor_a["doctor_id"], "slot": "2099-01-01T10:00", "status": "seen",
    }
    _save_appointments(appointments)
    try:
        from urllib.parse import quote
        as_owner = client.get(f"/api/doctor/patient-history/{quote(patient_name)}", headers={"Authorization": f"Bearer {token_a}"})
        assert as_owner.status_code == 200
        assert len(as_owner.json()["history"]) == 1

        as_other = client.get(f"/api/doctor/patient-history/{quote(patient_name)}", headers={"Authorization": f"Bearer {token_b}"})
        assert as_other.status_code == 200
        assert as_other.json()["history"] == []
    finally:
        appointments = _load_appointments()
        appointments.pop(appt_id, None)
        _save_appointments(appointments)


# ---------------------------------------------------------------------------
# Rate limiting: 6th login attempt in a minute → 429
# ---------------------------------------------------------------------------

def test_sixth_login_attempt_in_a_minute_is_rate_limited(client):
    email = _unique_email()
    statuses = []
    for _ in range(6):
        res = client.post("/api/auth/login", json={"email": email, "password": "wrong"})
        statuses.append(res.status_code)

    assert statuses[:5] == [401] * 5, statuses
    assert statuses[5] == 429, statuses
