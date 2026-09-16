"""Self-service account deactivation (diffdx/routers/account_deletion.py)
and the admin console's confirm_user_id branch it makes necessary
(admin_portal/routers/dsr.py's erase_subject).

This replaces an earlier version of this test module that covered a
self-service *full erase* route — that route (DELETE /api/patient/account)
no longer exists; deactivation (scrub + retain) is a different, shallower
operation with a different route (POST /api/patient/account/deactivate).

Isolation strategy: same as tests/test_auth.py — a private temp SQLite
engine, monkeypatched in via diffdx.db.engine.get_engine/get_sessionmaker
for the whole module, with PRAGMA foreign_keys=ON explicitly enabled (same
pattern tests/test_appointment_repositories.py uses) since these tests
care about ON DELETE CASCADE (refresh tokens, dsr_erasure_requests) firing
correctly — SQLite does not enforce foreign keys per-connection unless
this is set, unlike Postgres, which always does.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from diffdx.db import models  # noqa: F401 — registers all models on Base
from diffdx.db.base import Base
import diffdx.db.engine as db_engine

# Imported at module (collection) time, deliberately, rather than lazily
# inside the `client` fixture below — diffdx.legacy_store does
# `from diffdx.db.engine import get_sessionmaker` at ITS OWN top level, a
# frozen name binding to whatever db_engine.get_sessionmaker *is* at the
# moment web.api (and therefore legacy_store) first gets imported, for the
# rest of the process — reassigning db_engine.get_sessionmaker later (the
# _redirect_db fixture below) never reaches that already-frozen copy.
# Pytest imports every test module during collection, before any fixture
# (autouse included) from any file has run — so importing here guarantees
# that freeze captures the true, unpatched original function, the same
# invariant every other test module in this suite that uses db_engine's
# monkeypatch pattern silently depends on. Importing lazily inside a
# fixture instead risks this module being the first to trigger the import
# *after* its own _redirect_db has already monkeypatched
# db_engine.get_sessionmaker — freezing legacy_store to a lambda bound to
# this module's own (later-disposed) test engine, which would then leak
# into every other test module that also goes through
# legacy_store._user_from_access_token (i.e. any authenticated request).
from web.api import app as _app  # noqa: E402


@pytest.fixture(scope="module")
def test_engine(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("account_deletion_test") / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def test_sessionmaker(test_engine):
    return sessionmaker(bind=test_engine, expire_on_commit=False)


@pytest.fixture(scope="module", autouse=True)
def _redirect_db(test_engine, test_sessionmaker):
    orig_get_engine = db_engine.get_engine
    orig_get_sessionmaker = db_engine.get_sessionmaker
    db_engine.get_sessionmaker = lambda: test_sessionmaker
    db_engine.get_engine = lambda: test_engine
    yield
    db_engine.get_engine = orig_get_engine
    db_engine.get_sessionmaker = orig_get_sessionmaker
    # diffdx.legacy_store's own top-level `from diffdx.db.engine import
    # get_sessionmaker` is frozen to the TRUE original function (guaranteed
    # by importing web.api at module/collection time above, before this
    # fixture or any other module's fixtures have run). That original
    # function caches itself into diffdx.db.engine's own _engine/
    # _SessionLocal module globals the first time it's actually CALLED —
    # and every authenticated request this module makes calls it, while
    # get_engine()/get_sessionmaker() above are patched to THIS module's
    # test engine. Left cached, that would permanently poison every later
    # test module's own db_engine monkeypatch, since the original function
    # never recomputes once its cache is set. Clearing both after this
    # module's tests finish lets the next module that actually calls it
    # re-cache against its own active patch instead.
    db_engine._engine = None
    db_engine._SessionLocal = None


@pytest.fixture(scope="module")
def client():
    with TestClient(_app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from diffdx.rate_limit import limiter
    limiter.reset()
    yield


@pytest.fixture(autouse=True)
def _enable_self_service_flag(monkeypatch):
    """On by default for this module — the one test that needs it OFF
    (test_deactivate_501_when_flag_unset) opts out explicitly."""
    monkeypatch.setenv("SELF_SERVICE_ACCOUNT_DEACTIVATION_ENABLED", "true")
    yield


def _unique_email() -> str:
    return f"acct.deact.test.{uuid.uuid4()}@example.com"


def _register_patient(client, password: str = "testpass123") -> tuple[str, str, dict]:
    """Returns (access_token, refresh_token, user_dict)."""
    email = _unique_email()
    res = client.post("/api/auth/register", json={"name": "Deactivation Test", "email": email, "password": password})
    assert res.status_code == 200, res.text
    data = res.json()
    return data["access_token"], data["refresh_token"], data["user"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Deactivation preview
# ---------------------------------------------------------------------------

def test_deactivation_preview_requires_auth(client):
    res = client.get("/api/patient/account/deactivation-preview")
    assert res.status_code == 401


def test_deactivation_preview_returns_expected_shape(client):
    access_token, _refresh, _user = _register_patient(client)
    res = client.get("/api/patient/account/deactivation-preview", headers=_auth_headers(access_token))
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["will_cancel"] == {"future_appointments": 0}
    assert "password" in data["will_scrub"]
    labels = {c["label"] for c in data["will_retain"]}
    assert "Consultations" in labels
    assert "Appointments" in labels


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

def test_deactivate_501_when_flag_unset(client, monkeypatch):
    monkeypatch.delenv("SELF_SERVICE_ACCOUNT_DEACTIVATION_ENABLED", raising=False)
    access_token, _refresh, _user = _register_patient(client)
    res = client.post(
        "/api/patient/account/deactivate", headers=_auth_headers(access_token),
        json={"password": "testpass123", "confirm_email": "irrelevant@example.com"},
    )
    assert res.status_code == 501
    assert "SELF_SERVICE_ACCOUNT_DEACTIVATION_ENABLED" in res.json()["detail"]

    me = client.get("/api/auth/me", headers=_auth_headers(access_token))
    assert me.status_code == 200


# ---------------------------------------------------------------------------
# Wrong credentials — rejected, nothing changed
# ---------------------------------------------------------------------------

def test_deactivate_wrong_password_rejected(client):
    access_token, _refresh, user = _register_patient(client, password="correcthorse123")
    res = client.post(
        "/api/patient/account/deactivate", headers=_auth_headers(access_token),
        json={"password": "wrongpassword", "confirm_email": user["email"]},
    )
    assert res.status_code == 400
    assert "password" in res.json()["detail"].lower()

    me = client.get("/api/auth/me", headers=_auth_headers(access_token))
    assert me.status_code == 200
    assert me.json()["email"] == user["email"]


def test_deactivate_email_mismatch_rejected(client):
    access_token, _refresh, _user = _register_patient(client, password="correcthorse123")
    res = client.post(
        "/api/patient/account/deactivate", headers=_auth_headers(access_token),
        json={"password": "correcthorse123", "confirm_email": "someone.else@example.com"},
    )
    assert res.status_code == 400
    assert "email" in res.json()["detail"].lower()

    me = client.get("/api/auth/me", headers=_auth_headers(access_token))
    assert me.status_code == 200


# ---------------------------------------------------------------------------
# Full successful deactivation — the real end-to-end path
# ---------------------------------------------------------------------------

def test_deactivate_success_end_to_end(client, test_sessionmaker):
    from diffdx.db.models.audit import AuditLogEntry
    from diffdx.db.models.dsr import DsrErasureRequest
    from diffdx.db.models.user import Patient, RefreshToken, User
    from diffdx.legacy_store import _load_appointments, _save_appointments

    password = "correcthorse123"
    access_token, refresh_token, user = _register_patient(client, password=password)
    uid = uuid.UUID(user["id"])
    uid_str = str(uid)

    # Queue an in-app purge request first, to prove it survives
    # deactivation untouched (the two are independent entry points into
    # the eventual admin purge — deactivating must not silently cancel or
    # remove it).
    dsr_res = client.post("/api/patient/dsr-request", json={"reason": "testing"}, headers=_auth_headers(access_token))
    assert dsr_res.status_code == 200, dsr_res.text

    # A future "upcoming" appointment for this patient, blob-store only —
    # enough to exercise the auto-cancel path (see
    # _cancel_patient_appointment_impl's best-effort relational dual-write:
    # a nonexistent doctor_id here just makes that skip gracefully, it
    # never fails the cancel itself).
    appt_id = str(uuid.uuid4())
    appointments = _load_appointments()
    appointments[appt_id] = {
        "appointment_id": appt_id, "session_id": "",
        "patient_user_id": uid_str, "patient_name": user["name"],
        "doctor_id": "test_doc_nonexistent", "doctor_name": "Dr. Test",
        "specialty": "General", "slot": "2099-01-01T10:00",
        "status": "upcoming",
    }
    _save_appointments(appointments)

    try:
        res = client.post(
            "/api/patient/account/deactivate", headers=_auth_headers(access_token),
            json={"password": password, "confirm_email": user["email"]},
        )
        assert res.status_code == 200, res.text
        receipt = res.json()
        assert receipt["status"] == "ok"
        assert receipt["cancelled_appointments"] == 1
        assert receipt["removed"]["refresh_tokens"] == 1
        assert receipt["audit_entry_id"]

        # The blob appointment was actually cancelled, with the distinct
        # cancelled_by value.
        appts_after = _load_appointments()
        assert appts_after[appt_id]["status"] == "cancelled"
        assert appts_after[appt_id]["cancelled_by"] == "patient_deactivation"
    finally:
        appts_cleanup = _load_appointments()
        appts_cleanup.pop(appt_id, None)
        _save_appointments(appts_cleanup)

    # The User row still exists (NOT deleted) — scrubbed in place.
    with test_sessionmaker() as db:
        row = db.get(User, uid)
        assert row is not None
        assert row.deleted_at is not None
        assert row.name == "Deactivated Patient"
        assert row.email == f"erased+{uid}@invalid"
        # Contact PII scrubbed, clinical fields untouched (none were set
        # here, but the columns must exist / not error).
        patient_row = db.get(Patient, uid)
        assert patient_row is not None
        assert patient_row.mobile is None
        assert patient_row.address is None

    # DsrErasureRequest row is untouched — still exists, still pending.
    with test_sessionmaker() as db:
        req = db.execute(select(DsrErasureRequest).where(DsrErasureRequest.user_id == uid)).scalar_one()
        assert req.status == "pending"

    # Every refresh token for this user is gone (deleted, not merely
    # revoked).
    with test_sessionmaker() as db:
        remaining_tokens = db.execute(select(RefreshToken).where(RefreshToken.user_id == uid)).scalars().all()
        assert remaining_tokens == []

    # The audit entry exists with the SAME (unpseudonymised) uid as actor —
    # deactivation, unlike the old erase path, never changes the subject's
    # id, so no pseudonym-sweep applies here.
    with test_sessionmaker() as db:
        entry = db.execute(
            select(AuditLogEntry)
            .where(AuditLogEntry.action == "subject_self_deactivation", AuditLogEntry.resource_id == uid_str)
        ).scalar_one()
        assert entry.actor_user_id == uid

    # The access token itself remains cryptographically valid until it
    # naturally expires — deactivation, unlike the old full-erase path,
    # does not delete the user row, so _user_from_access_token's
    # get_by_id lookup still finds it, now scrubbed. /me correctly
    # reflects the scrubbed identity rather than erroring.
    me = client.get("/api/auth/me", headers=_auth_headers(access_token))
    assert me.status_code == 200
    assert me.json()["email"] == f"erased+{uid}@invalid"
    assert me.json()["name"] == "Deactivated Patient"

    # But no NEW session can be minted: the refresh token is gone, and
    # logging in fresh is impossible since password_hash was overwritten.

    refresh_res = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_res.status_code == 401

    login_res = client.post("/api/auth/login", json={"email": user["email"], "password": password})
    assert login_res.status_code == 401


def test_deactivate_twice_rejected(client):
    password = "correcthorse123"
    access_token, _refresh, user = _register_patient(client, password=password)
    first = client.post(
        "/api/patient/account/deactivate", headers=_auth_headers(access_token),
        json={"password": password, "confirm_email": user["email"]},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/patient/account/deactivate", headers=_auth_headers(access_token),
        json={"password": password, "confirm_email": user["email"]},
    )
    assert second.status_code == 400
    assert "already deactivated" in second.json()["detail"].lower()


# ---------------------------------------------------------------------------
# uid is never accepted from the request — always the authenticated caller
# ---------------------------------------------------------------------------

def test_deactivation_preview_ignores_any_id_like_query_param(client):
    access_token, _refresh, _user = _register_patient(client)
    other_uid = str(uuid.uuid4())
    res = client.get(f"/api/patient/account/deactivation-preview?user_id={other_uid}", headers=_auth_headers(access_token))
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Admin console: confirm_email vs confirm_user_id branch
# ---------------------------------------------------------------------------

def _make_admin(test_sessionmaker, client) -> tuple[str, str]:
    """Seeds an admin account directly (same technique scripts/seed_admin.py
    uses), then logs in through the real endpoint to get a JWT — no shortcut
    around the actual auth path."""
    from diffdx.legacy_store import _hash_password
    from diffdx.repositories.users import UserRepository

    email = f"admin.test.{uuid.uuid4()}@example.com"
    password = "adminpass123"
    with test_sessionmaker() as db:
        UserRepository(db).shadow_user(
            id=uuid.uuid4(), name="Admin Test", email=email, password_hash=_hash_password(password), role="admin",
        )
        db.commit()

    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["access_token"], email


def test_admin_erase_requires_confirm_email_for_active_subject(client, test_sessionmaker):
    admin_token, _email = _make_admin(test_sessionmaker, client)
    _access_token, _refresh, patient = _register_patient(client)

    # confirm_user_id alone does NOT satisfy an active (non-deactivated)
    # subject's check — confirm_email is still required for them,
    # unchanged from before this feature existed.
    res = client.request(
        "DELETE", f"/api/admin/subjects/{patient['id']}?dry_run=true",
        headers=_auth_headers(admin_token), json={"confirm_user_id": patient["id"]},
    )
    assert res.status_code == 400
    assert "confirm_email" in res.json()["detail"]

    ok = client.request(
        "DELETE", f"/api/admin/subjects/{patient['id']}?dry_run=true",
        headers=_auth_headers(admin_token), json={"confirm_email": patient["email"]},
    )
    assert ok.status_code == 200, ok.text


def test_admin_erase_requires_confirm_user_id_for_deactivated_subject(client, test_sessionmaker):
    admin_token, _email = _make_admin(test_sessionmaker, client)
    password = "correcthorse123"
    access_token, _refresh, patient = _register_patient(client, password=password)

    deact = client.post(
        "/api/patient/account/deactivate", headers=_auth_headers(access_token),
        json={"password": password, "confirm_email": patient["email"]},
    )
    assert deact.status_code == 200, deact.text

    # The subject's real email no longer exists — sending confirm_email
    # (even the correct pre-scrub value) has no effect for a deactivated
    # subject; the branch only ever looks at confirm_user_id for them, so
    # this is rejected the same as any other missing/wrong confirm_user_id.
    wrong = client.request(
        "DELETE", f"/api/admin/subjects/{patient['id']}?dry_run=true",
        headers=_auth_headers(admin_token), json={"confirm_email": patient["email"]},
    )
    assert wrong.status_code == 400
    assert "confirm_user_id" in wrong.json()["detail"]

    # A mismatched confirm_user_id is also rejected.
    mismatched = client.request(
        "DELETE", f"/api/admin/subjects/{patient['id']}?dry_run=true",
        headers=_auth_headers(admin_token), json={"confirm_user_id": str(uuid.uuid4())},
    )
    assert mismatched.status_code == 400
    assert "confirm_user_id" in mismatched.json()["detail"]

    # The correct id succeeds.
    ok = client.request(
        "DELETE", f"/api/admin/subjects/{patient['id']}?dry_run=true",
        headers=_auth_headers(admin_token), json={"confirm_user_id": patient["id"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["dry_run"] is True


def test_search_subjects_includes_deactivated_flag(client, test_sessionmaker):
    admin_token, _email = _make_admin(test_sessionmaker, client)
    password = "correcthorse123"
    access_token, _refresh, patient = _register_patient(client, password=password)

    before = client.get(f"/api/admin/subjects?q={patient['email']}", headers=_auth_headers(admin_token))
    assert before.status_code == 200
    before_item = next(i for i in before.json()["items"] if i["user_id"] == patient["id"])
    assert before_item["deactivated"] is False

    deact = client.post(
        "/api/patient/account/deactivate", headers=_auth_headers(access_token),
        json={"password": password, "confirm_email": patient["email"]},
    )
    assert deact.status_code == 200, deact.text

    # Search by the ORIGINAL email no longer matches (scrubbed) — that's
    # expected; find it by user_id via a fresh unfiltered-enough search
    # instead, confirming the item is still present at all (not excluded)
    # and correctly flagged.
    after = client.get("/api/admin/subjects", headers=_auth_headers(admin_token))
    assert after.status_code == 200
    after_item = next(i for i in after.json()["items"] if i["user_id"] == patient["id"])
    assert after_item["deactivated"] is True
    assert after_item["name"] == "Deactivated Patient"


def test_inventory_still_correct_after_deactivation(client, test_sessionmaker):
    admin_token, _email = _make_admin(test_sessionmaker, client)
    password = "correcthorse123"
    access_token, _refresh, patient = _register_patient(client, password=password)

    deact = client.post(
        "/api/patient/account/deactivate", headers=_auth_headers(access_token),
        json={"password": password, "confirm_email": patient["email"]},
    )
    assert deact.status_code == 200, deact.text

    inv = client.get(f"/api/admin/subjects/{patient['id']}/inventory", headers=_auth_headers(admin_token))
    assert inv.status_code == 200, inv.text
    data = inv.json()
    assert data["deactivated"] is True
    labels = {c["label"]: c for c in data["categories"]}
    assert labels["Identity"]["record_count"] == 1
