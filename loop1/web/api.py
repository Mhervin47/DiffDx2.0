"""
DiffDx Web API — FastAPI backend wrapping APISession.

Endpoints:
  GET  /api/cases                        → list available test cases
  GET  /api/cases/{case_id}              → full case profile JSON
  POST /api/session/start                → start session, get first question
  POST /api/session/{session_id}/turn    → submit answer, get next question + critique
  GET  /api/session/{session_id}/report  → full critic report (once complete)

Static files served from web/static/ at the root path.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure src/ is on the path before importing loop1 modules
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root / "src"))

# Load .env
try:
    import dotenv
    dotenv.load_dotenv(_repo_root / ".env")
except ImportError:
    pass

os.environ.setdefault("CRITIC_MODEL", "openrouter/google/gemma-4-31b-it:free")

import asyncio
import threading
import httpx

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

from diffdx.api_exceptions import register_exception_handlers
from diffdx.audit import log_audit_event
from diffdx.config import settings as _settings
from diffdx.db.engine import get_sessionmaker
from diffdx.exceptions import ConflictError
from diffdx.rate_limit import limiter as _limiter
from diffdx.repositories.appointments import AppointmentRepository
from diffdx.repositories.users import UserRepository
from loop1.schemas import Demographics, History, PatientProfile, Symptom
from loop3.routing.router import route as compute_routing
from web.api_session import APISession

from diffdx.routers.appointments import router as appointments_router
from diffdx.routers.appointments2 import router as appointments2_router
from diffdx.routers.appointments3 import router as appointments3_router
from diffdx.routers.appointments4 import router as appointments4_router
from diffdx.routers.appointments5 import router as appointments5_router
from diffdx.routers.appointments6 import router as appointments6_router
from diffdx.routers.auth import router as auth_router
from diffdx.routers.doctors import router as doctors_router
from diffdx.routers.messaging import router as messaging_router
from diffdx.routers.pages import router as pages_router
from diffdx.routers.session_test_files import router as session_test_files_router
from diffdx.routers.session_booking import router as session_booking_router
from diffdx.routers.sessions import router as sessions_router

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="DiffDx API", version="0.10.0")

# Task 5: allow_origins=["*"] on an app handling patient data is
# indefensible — now reads from CORS_ALLOWED_ORIGINS (comma-separated),
# defaulting to "*" only because that's harmless for local dev; config.py
# refuses to start in production with the wildcard still set.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Task 5: rate limit /api/auth/login and /api/auth/register (5/minute per
# IP, see routers/auth.py's @limiter.limit decorators) — everything else
# is unlimited, this isn't a blanket API rate limiter.
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Task 2 wrote this to map the repository layer's typed errors to HTTP
# responses (ConflictError -> 409, OperationalError -> 503) but never wired
# it up — nothing routed through the repositories yet at the time. Now that
# routers (identity cutover, and this task's booking-race fix) actually
# call into diffdx.repositories.*, wire it up for real.
register_exception_handlers(app)

class AuditLogMiddleware(BaseHTTPMiddleware):
    """Task 5: write an AuditLogEntry for every PHI read and appointment
    mutation. login/login_failed are logged directly in routers/auth.py
    (they aren't URL patterns this middleware would recognize as
    PHI-bearing); this covers the rest by URL pattern instead of
    instrumenting every one of the ~90 route handlers individually —
    same coverage, far less surface to get wrong or miss one on.

    Deliberately coarse: action is `{method} {path}` (with the numbered
    id path segment kept, e.g. "PATCH /api/doctor/appointments/{id}/tags"
    would be nice but Starlette doesn't expose the *matched route
    template* from inside BaseHTTPMiddleware, only the resolved path with
    real values — using the real appt_id as resource_id instead covers
    the same need: "which appointment did doctor X touch, and when").
    Only fires on a successful (< 400) response — a 401/403/404 didn't
    actually read or mutate anything.
    """

    _PHI_PATH_PREFIXES = (
        "/api/doctor/appointments",
        "/api/patient/appointments",
        "/api/doctor/patient-history",
        "/api/doctor/pending-refills",
        "/api/doctor/renewal-reminders",
        "/api/doctor/second-opinions",
        "/api/session/",  # report/routing/suggested-tests/book all carry PHI
    )

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if response.status_code < 400 and any(path.startswith(p) for p in self._PHI_PATH_PREFIXES):
            try:
                user = _get_user_from_request(request)
                resource_id = next(iter(request.path_params.values()), None)
                log_audit_event(
                    actor=user,
                    action=f"{request.method} {path}",
                    resource_type="appointment" if "appointment" in path or "patient-history" in path else "session",
                    resource_id=str(resource_id) if resource_id else None,
                    ip_address=request.client.host if request.client else None,
                )
            except Exception:
                _log.warning("Audit log middleware failed for %s %s", request.method, path, exc_info=True)
        return response


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """Add no-cache headers to all .html and .js responses so browsers always fetch fresh files."""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.endswith((".html", ".js")) or path == "/":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

app.add_middleware(NoCacheStaticMiddleware)
app.add_middleware(AuditLogMiddleware)

# Task 4 (split the monolith): routers peeled off one domain at a time.
# See TASK4_SPLIT_ROUTERS.md for what's moved and what's still here.
app.include_router(appointments_router)
app.include_router(appointments2_router)
app.include_router(appointments3_router)
app.include_router(appointments4_router)
app.include_router(appointments5_router)
app.include_router(appointments6_router)
app.include_router(auth_router)
app.include_router(doctors_router)
app.include_router(messaging_router)
app.include_router(pages_router)
app.include_router(session_test_files_router)
app.include_router(sessions_router)
app.include_router(session_booking_router)

# In-memory session store (sufficient for local demo)
_sessions: dict[str, APISession] = {}

# Seed doctor accounts once at startup; start reminder scheduler
@app.on_event("startup")
def _on_startup():
    _seed_doctor_accounts()
    _start_reminder_scheduler()


# ---------------------------------------------------------------------------
# Appointment reminder emails (Resend)
# ---------------------------------------------------------------------------
def _send_reminder_email(to_email: str, patient_name: str, doctor_name: str, slot: str) -> bool:
    """Send a 24h appointment reminder via Resend. Returns True on success."""
    resend_key = os.environ.get("RESEND_API_KEY", "")
    from_addr  = os.environ.get("RESEND_FROM", "reminders@diffdx.app")
    if not resend_key:
        _log.info("RESEND_API_KEY not set — skipping reminder email to %s", to_email)
        return False
    try:
        import urllib.request
        slot_fmt = datetime.fromisoformat(slot).strftime("%A, %d %B %Y at %H:%M")
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:32px 24px;background:#f8fafc;border-radius:12px;">
          <div style="background:#0a2540;border-radius:10px 10px 0 0;padding:24px 28px;">
            <h1 style="color:#fff;font-size:20px;margin:0;font-weight:800;">DiffDx</h1>
            <p style="color:rgba(255,255,255,.7);font-size:13px;margin:6px 0 0;">Appointment Reminder</p>
          </div>
          <div style="background:#fff;border-radius:0 0 10px 10px;padding:28px;border:1px solid #e2ecf4;border-top:none;">
            <p style="font-size:16px;color:#0a2540;font-weight:600;">Hi {patient_name},</p>
            <p style="font-size:14px;color:#334155;line-height:1.6;">
              This is a reminder that you have an appointment tomorrow with
              <strong>{doctor_name}</strong>.
            </p>
            <div style="background:#f0f9ff;border:1px solid #d0e4ef;border-radius:8px;padding:16px 20px;margin:20px 0;">
              <div style="font-size:11px;color:#0077a8;font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px;">Appointment Time</div>
              <div style="font-size:17px;font-weight:700;color:#0a2540;">{slot_fmt}</div>
            </div>
            <p style="font-size:13px;color:#64748b;line-height:1.6;">
              Please ensure you have completed any required pre-appointment tests and bring all relevant documents.
            </p>
            <a href="https://diffdx.app/my-sessions.html" style="display:inline-block;background:#0077a8;color:#fff;text-decoration:none;padding:12px 24px;border-radius:8px;font-size:14px;font-weight:700;margin-top:8px;">View My Sessions</a>
          </div>
          <p style="font-size:11px;color:#94a3b8;text-align:center;margin-top:16px;">DiffDx · This is an automated reminder</p>
        </div>"""
        payload = json.dumps({
            "from": from_addr,
            "to": [to_email],
            "subject": f"Reminder: Appointment tomorrow with {doctor_name}",
            "html": html,
        }).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status < 300
    except Exception as exc:
        _log.warning("Reminder email failed for %s: %s", to_email, exc)
        return False


def _reminder_loop():
    """Background thread: every hour check for appointments 24±2h away and send one reminder."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            window_start = (now + timedelta(hours=22)).isoformat()
            window_end   = (now + timedelta(hours=26)).isoformat()
            appointments = _load_appointments()
            users = _load_users()
            uid_map = {u["id"]: u for u in users.values() if isinstance(u, dict)}
            changed = False
            for appt_id, appt in appointments.items():
                slot = appt.get("slot", "")
                if not slot:
                    continue
                if not (window_start[:16] <= slot[:16] <= window_end[:16]):
                    continue
                if appt.get("reminder_sent"):
                    continue
                patient_uid = appt.get("patient_user_id")
                patient = uid_map.get(patient_uid, {})
                email = patient.get("email", "")
                if not email:
                    continue
                ok = _send_reminder_email(
                    to_email=email,
                    patient_name=patient.get("name", "Patient"),
                    doctor_name=appt.get("doctor_name", "your doctor"),
                    slot=slot,
                )
                if ok:
                    appt["reminder_sent"] = True
                    changed = True
                    _log.info("Reminder sent for appt %s to %s", appt_id, email)
            if changed:
                _save_appointments(appointments)
        except Exception as exc:
            _log.warning("Reminder loop error: %s", exc)
        threading.Event().wait(3600)  # run once per hour


def _start_reminder_scheduler():
    t = threading.Thread(target=_reminder_loop, daemon=True, name="reminder-scheduler")
    t.start()
    _log.info("Appointment reminder scheduler started")

_CASES_DIR = _repo_root / "test_cases"

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

# This domain's request schemas all moved to diffdx/schemas/appointments.py
# as part of Task 4's router split. As of appointments6.py (the domain's
# last router), every route that used one of these has moved out of this
# file, so nothing needs to be imported back here anymore.


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# PostgreSQL data store — single table, one collection per row
# Reads DATABASE_URL from env (Railway sets this automatically).
# Falls back to SQLite for local dev if DATABASE_URL is not set.
# ---------------------------------------------------------------------------
import os
import sqlite3

_DATA_DIR = _repo_root / "web" / "data"
_DB_FILE   = _DATA_DIR / "diffdx.db"

# Legacy JSON paths — kept so the migration script can find them
_USERS_FILE           = _DATA_DIR / "users.json"
_DOCTORS_FILE         = _DATA_DIR / "doctors.json"
_APPOINTMENTS_FILE    = _DATA_DIR / "appointments.json"
_SESSION_UPLOADS_FILE = _DATA_DIR / "session_uploads.json"
_MESSAGES_FILE        = _DATA_DIR / "messages.json"
_WAITLIST_FILE        = _DATA_DIR / "waitlist.json"
_BLOCKED_DATES_FILE   = _DATA_DIR / "blocked_dates.json"
_SECOND_OPINIONS_FILE = _DATA_DIR / "second_opinions.json"

_DATABASE_URL: str | None = os.environ.get("DATABASE_URL")
_USE_POSTGRES: bool = bool(_DATABASE_URL)

# ── Postgres connection pool ──────────────────────────────────────────────
_pg_pool = None   # psycopg2 SimpleConnectionPool


def _parse_pg_url(url: str) -> dict:
    """Parse a postgres(ql):// URL into psycopg2 keyword args.

    Handles passwords that contain '@' by splitting on the *last* '@'
    before the host portion.
    """
    url = url.strip()
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    # Split credentials from host on the last '@'
    at = url.rfind("@")
    creds, hostpart = url[:at], url[at + 1:]
    colon = creds.find(":")
    user = creds[:colon]
    password = creds[colon + 1:]
    # hostpart may be  host/dbname  or  host:port/dbname
    if "/" in hostpart:
        hostport, dbname = hostpart.split("/", 1)
    else:
        hostport, dbname = hostpart, "postgres"
    if ":" in hostport:
        host, port_str = hostport.rsplit(":", 1)
        port = int(port_str)
    else:
        host, port = hostport, 5432
    return dict(host=host, port=port, dbname=dbname, user=user, password=password, sslmode="require")


def _get_pg():
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    import psycopg2
    from psycopg2 import pool as pg_pool
    kwargs = _parse_pg_url(_DATABASE_URL)
    _pg_pool = pg_pool.ThreadedConnectionPool(1, 10, **kwargs)
    # Ensure table exists
    conn = _pg_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS store (
                    collection TEXT PRIMARY KEY,
                    data       TEXT NOT NULL
                )
            """)
        conn.commit()
    finally:
        _pg_pool.putconn(conn)
    return _pg_pool


# ── SQLite connection (local dev fallback) ────────────────────────────────
_sqlite_conn: sqlite3.Connection | None = None


def _get_sqlite() -> sqlite3.Connection:
    global _sqlite_conn
    if _sqlite_conn is not None:
        return _sqlite_conn
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_FILE), check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS store (
            collection TEXT PRIMARY KEY,
            data       TEXT NOT NULL
        )
    """)
    conn.commit()
    _sqlite_conn = conn
    return conn


# ── Unified helpers ───────────────────────────────────────────────────────

def _db_load(collection: str, default):
    try:
        if _USE_POSTGRES:
            pool = _get_pg()
            conn = pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT data FROM store WHERE collection=%s", (collection,))
                    row = cur.fetchone()
                return json.loads(row[0]) if row else default
            finally:
                pool.putconn(conn)
        else:
            row = _get_sqlite().execute(
                "SELECT data FROM store WHERE collection=?", (collection,)
            ).fetchone()
            return json.loads(row[0]) if row else default
    except Exception:
        return default


def _db_save(collection: str, data) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    if _USE_POSTGRES:
        pool = _get_pg()
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO store (collection, data) VALUES (%s, %s)
                    ON CONFLICT (collection) DO UPDATE SET data = EXCLUDED.data
                """, (collection, payload))
            conn.commit()
        finally:
            pool.putconn(conn)
    else:
        db = _get_sqlite()
        db.execute(
            "INSERT OR REPLACE INTO store (collection, data) VALUES (?, ?)",
            (collection, payload),
        )
        db.commit()
# ── Sarvam AI helpers ──────────────────────────────────────────────────────────
_SARVAM_KEY = os.environ.get("SARVAM_API_KEY", "")
_SARVAM_TRANSLATE_URL = "https://api.sarvam.ai/translate"
_SARVAM_TTS_URL       = "https://api.sarvam.ai/text-to-speech"

# BCP-47 → Sarvam language code mapping
_SARVAM_LANG = {
    "hi-IN": "hi-IN", "ta-IN": "ta-IN", "te-IN": "te-IN",
    "kn-IN": "kn-IN", "ml-IN": "ml-IN", "bn-IN": "bn-IN",
    "mr-IN": "mr-IN", "gu-IN": "gu-IN", "pa-IN": "pa-IN",
    "en-IN": "en-IN",
}

# bulbul:v2 compatible speakers only: anushka, abhilash, manisha, vidya, arya, karun, hitesh
_SARVAM_SPEAKER = {
    "hi-IN": "anushka", "ta-IN": "anushka", "te-IN": "manisha",
    "kn-IN": "vidya",   "ml-IN": "arya",    "bn-IN": "manisha",
    "mr-IN": "vidya",   "gu-IN": "anushka", "pa-IN": "anushka",
    "en-IN": "anushka",
}


def _sarvam_translate(text: str, source_lang: str, target_lang: str) -> str:
    """Translate text via Sarvam API. Returns original text on any failure."""
    if not _SARVAM_KEY or not text.strip():
        return text
    try:
        import httpx
        resp = httpx.post(
            _SARVAM_TRANSLATE_URL,
            headers={"api-subscription-key": _SARVAM_KEY, "Content-Type": "application/json"},
            json={
                "input": text,
                "source_language_code": _SARVAM_LANG.get(source_lang, "en-IN"),
                "target_language_code": _SARVAM_LANG.get(target_lang, "hi-IN"),
                "speaker_gender": "Female",
                "mode": "formal",
                "model": "mayura:v1",
                "enable_preprocessing": True,
            },
            timeout=15.0,
        )
        if resp.status_code == 200:
            return resp.json().get("translated_text", text)
        _log.warning("Sarvam translate %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        _log.warning("Sarvam translate failed: %s", exc)
    return text


def _sarvam_tts_b64(text: str, lang: str) -> str | None:
    """Call Sarvam TTS and return base64 WAV audio, or None on failure."""
    if not _SARVAM_KEY or not text.strip():
        return None
    try:
        import httpx
        resp = httpx.post(
            _SARVAM_TTS_URL,
            headers={"api-subscription-key": _SARVAM_KEY, "Content-Type": "application/json"},
            json={
                "inputs": [text[:500]],
                "target_language_code": _SARVAM_LANG.get(lang, "hi-IN"),
                "speaker": _SARVAM_SPEAKER.get(lang, "anushka"),
                "model": "bulbul:v2",
                "enable_preprocessing": True,
            },
            timeout=20.0,
        )
        if resp.status_code == 200:
            audios = resp.json().get("audios", [])
            return audios[0] if audios else None
        _log.warning("Sarvam TTS %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        _log.warning("Sarvam TTS failed: %s", exc)
    return None


def _load_user_sessions(user_id: str) -> list:
    """Diagnostic-session summaries for a user. Kept as its own blob
    collection (not identity data) so it can keep working after `users`
    moves to the relational store — see _compose_user_dict/
    _add_session_to_user/_update_session_in_user."""
    return _db_load("user_sessions", {}).get(user_id, [])


def _save_user_sessions(user_id: str, sessions: list) -> None:
    all_sessions = _db_load("user_sessions", {})
    all_sessions[user_id] = sessions
    _db_save("user_sessions", all_sessions)


def _compose_user_dict(db, dto) -> dict:
    """Build the legacy blob-shaped user dict from relational rows, so the
    ~55 call sites that read `user["..."]`/`user.get("...")` off
    `_load_users()`/`_get_user_from_request()` don't need to change —
    same pattern Task 5 used to keep `_get_user_from_request`'s return
    shape stable through the blob-token → JWT swap."""
    user_id_str = str(dto.id)
    dependents = UserRepository(db).list_dependents(dto.id)
    return {
        "id": user_id_str,
        "name": dto.name,
        "email": dto.email,
        "password_hash": dto.password_hash,
        "role": dto.role,
        "created_at": dto.created_at.isoformat() if dto.created_at else None,
        "doctor_id": dto.doctor_id,
        "specialty": dto.specialty,
        "mobile": dto.mobile,
        "age": dto.age,
        "blood_type": dto.blood_type,
        "gender": dto.gender,
        "address": dto.address,
        "emergency_contact_name": dto.emergency_contact_name,
        "emergency_contact_phone": dto.emergency_contact_phone,
        "allergies": dto.allergies,
        "chronic_conditions": dto.chronic_conditions,
        "dependents": [
            {
                "id": str(dep.id),
                "name": dep.name,
                "relationship": dep.relationship,
                "age": dep.age,
                "gender": dep.gender,
                "blood_type": dep.blood_type,
                "allergies": dep.allergies,
                "chronic_conditions": dep.chronic_conditions,
            }
            for dep in dependents
        ],
        "sessions": _load_user_sessions(user_id_str),
    }


def _load_users() -> dict:
    with get_sessionmaker()() as db:
        return {str(dto.id): _compose_user_dict(db, dto) for dto in UserRepository(db).list_all()}


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}:{key.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, key_hex = stored.split(":", 1)
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
        return secrets.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


def _user_from_access_token(token: str) -> dict | None:
    """Decode a JWT access token (Task 5) and return the (relationally
    composed, blob-shaped) user dict it names, or None if the token is
    missing/expired/invalid, or names a user that no longer exists. No
    server-side token table lookup needed — the JWT's signature is the
    credential, its `sub` claim is the user id. This is the hot path
    (every authenticated request), so it does a single indexed row fetch,
    not a full `_load_users()` scan. Exposed (not just used internally)
    because routers/appointments4.py's download_patient_file accepts a
    token via `?token=` query param as well as a Bearer header, for
    direct-link downloads that can't set an Authorization header."""
    import jwt as _pyjwt
    from diffdx.auth_tokens import decode_access_token

    try:
        claims = decode_access_token(token)
    except _pyjwt.PyJWTError:
        return None
    user_id = claims.get("sub")
    if not user_id:
        return None
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        return None
    with get_sessionmaker()() as db:
        dto = UserRepository(db).get_by_id(uid)
        if dto is None:
            return None
        return _compose_user_dict(db, dto)


def _get_user_from_request(request: Request) -> dict | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return _user_from_access_token(auth[7:])


def _add_session_to_user(user_id: str, session_meta: dict) -> None:
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        return
    with get_sessionmaker()() as db:
        if UserRepository(db).get_by_id(uid) is None:
            return
    sessions = _load_user_sessions(user_id)
    sessions.append(session_meta)
    _save_user_sessions(user_id, sessions)


def _update_session_in_user(user_id: str, session_id: str, diagnosis: str, ended_at: str) -> None:
    sessions = _load_user_sessions(user_id)
    for s in sessions:
        if s.get("session_id") == session_id:
            s["primary_diagnosis"] = diagnosis
            s["ended_at"] = ended_at
            s["status"] = "complete"
            break
    _save_user_sessions(user_id, sessions)


def _load_doctors() -> list:
    return _db_load("doctors", [])


def _save_doctors(doctors: list) -> None:
    _db_save("doctors", doctors)


def _load_appointments() -> dict:
    return _db_load("appointments", {})


def _save_appointments(appointments: dict) -> None:
    _db_save("appointments", appointments)


def _ensure_relational_appointment(db, appt: dict) -> uuid.UUID | None:
    """Phase B of the appointments cutover (dual-write, see
    TASK9_APPOINTMENTS_DUAL_WRITE_CORE.md): every dual-writing route needs a
    real Appointment row to attach its sub-entity write to (SuggestedTest/
    Referral/etc. all have a NOT NULL FK to appointments.id). Only bookings
    made after Task 7 (or backfilled by the migration script) have one —
    this self-heals the gap by creating the missing row from the blob
    record's own fields, so every appointment a dual-written route touches
    ends up shadowed over time, not just ones booked after Task 7 shipped.

    Best-effort by design: returns None (never raises) if the row can't be
    resolved or created — callers must treat that as "skip the dual-write
    for this request," since a shadow-write failure must never affect the
    blob write it's alongside."""
    appt_id_str = appt.get("appointment_id")
    try:
        appt_uuid = uuid.UUID(appt_id_str)
    except (TypeError, ValueError):
        return None

    existing = AppointmentRepository(db).get_by_id(appt_uuid)
    if existing is not None:
        return appt_uuid

    patient_dto = UserRepository(db).get_by_id(uuid.UUID(appt["patient_user_id"])) if appt.get("patient_user_id") else None
    doctor_dto = UserRepository(db).get_by_doctor_id(appt["doctor_id"]) if appt.get("doctor_id") else None
    if patient_dto is None or doctor_dto is None:
        _log.warning("Could not self-heal relational Appointment %s: patient or doctor not resolvable.", appt_id_str)
        return None

    slot_str = appt.get("slot")
    try:
        slot_dt = datetime.fromisoformat(slot_str)
    except (TypeError, ValueError):
        _log.warning("Could not self-heal relational Appointment %s: unparseable slot %r.", appt_id_str, slot_str)
        return None
    if slot_dt.tzinfo is None:
        slot_dt = slot_dt.replace(tzinfo=timezone.utc)

    booked_at = None
    try:
        booked_at_str = appt.get("booked_at")
        if booked_at_str:
            booked_at = datetime.fromisoformat(booked_at_str)
            if booked_at.tzinfo is None:
                booked_at = booked_at.replace(tzinfo=timezone.utc)
    except ValueError:
        booked_at = None

    try:
        AppointmentRepository(db).book(
            id=appt_uuid,
            patient_id=patient_dto.id,
            doctor_id=doctor_dto.id,
            slot_datetime=slot_dt,
            session_id=appt.get("session_id"),
            status=appt.get("status", "upcoming"),
            urgency=appt.get("urgency", "routine"),
            booked_at=booked_at,
            chief_complaint=appt.get("chief_complaint"),
            primary_diagnosis=appt.get("primary_diagnosis"),
            patient_age=appt.get("age"),
            patient_sex=appt.get("sex"),
            patient_bmi=appt.get("bmi"),
        )
    except ConflictError:
        _log.warning("Could not self-heal relational Appointment %s: slot conflict against existing data.", appt_id_str)
        return None
    return appt_uuid


def _load_session_uploads() -> dict:
    return _db_load("session_uploads", {})


def _save_session_uploads(data: dict) -> None:
    _db_save("session_uploads", data)


# ── Session report DB storage (survives server restarts / ephemeral filesystems) ─
def _save_session_report(session_id: str, report: dict) -> None:
    _db_save(f"session_report:{session_id}", report)


def _load_session_report_from_db(session_id: str) -> dict | None:
    return _db_load(f"session_report:{session_id}", None)


# ── File data stored separately from appointments (keeps appointments blob small) ─
def _save_file_data(appt_id: str, filename: str, data_b64: str) -> None:
    _db_save(f"file:{appt_id}:{filename}", {"data_b64": data_b64})


def _load_file_data(appt_id: str, filename: str) -> str | None:
    rec = _db_load(f"file:{appt_id}:{filename}", None)
    return rec["data_b64"] if rec else None


_DOCTOR_SEED = [
    {
        "doctor_id": "dr_001",
        "name": "Dr. Sarah Chen",
        "email": "sarah.chen@cityheart.com",
        "specialty": "Cardiology",
    },
    {
        "doctor_id": "dr_006",
        "name": "Dr. Amir Patel",
        "email": "amir.patel@brainspine.com",
        "specialty": "Neurology",
    },
    {
        "doctor_id": "dr_004",
        "name": "Dr. James Okafor",
        "email": "james.okafor@chestlung.com",
        "specialty": "Pulmonology",
    },
    {
        "doctor_id": "dr_008",
        "name": "Dr. Liam Torres",
        "email": "liam.torres@digestivehealth.com",
        "specialty": "Gastroenterology",
    },
    {
        "doctor_id": "dr_011",
        "name": "Dr. Samuel Obi",
        "email": "samuel.obi@westsideclinic.com",
        "specialty": "Internal Medicine",
    },
    {"doctor_id": "dr_025", "name": "Dr. Sarah Johnson",    "email": "sarah.johnson@medicenter.com",    "specialty": "General Practice"},
    {"doctor_id": "dr_026", "name": "Dr. Michael Chen",     "email": "michael.chen@medicenter.com",     "specialty": "General Practice"},
    {"doctor_id": "dr_027", "name": "Dr. Emily Davis",      "email": "emily.davis@medicenter.com",      "specialty": "General Practice"},
    {"doctor_id": "dr_028", "name": "Dr. Robert Martinez",  "email": "robert.martinez@heartcare.com",   "specialty": "Cardiology"},
    {"doctor_id": "dr_029", "name": "Dr. Lisa Thompson",    "email": "lisa.thompson@heartcare.com",     "specialty": "Cardiology"},
    {"doctor_id": "dr_030", "name": "Dr. James Wilson",     "email": "james.wilson@heartcare.com",      "specialty": "Cardiology"},
    {"doctor_id": "dr_031", "name": "Dr. Amanda Rodriguez", "email": "amanda.rodriguez@uroclinic.com",  "specialty": "Urology"},
    {"doctor_id": "dr_032", "name": "Dr. Kevin Brown",      "email": "kevin.brown@uroclinic.com",       "specialty": "Urology"},
    {"doctor_id": "dr_033", "name": "Dr. Jennifer Lee",     "email": "jennifer.lee@uroclinic.com",      "specialty": "Urology"},
    {"doctor_id": "dr_034", "name": "Dr. Maria Garcia",     "email": "maria.garcia@womenshealth.com",   "specialty": "Gynecology"},
    {"doctor_id": "dr_035", "name": "Dr. Rachel Kim",       "email": "rachel.kim@womenshealth.com",     "specialty": "Gynecology"},
    {"doctor_id": "dr_036", "name": "Dr. Catherine White",  "email": "catherine.white@womenshealth.com","specialty": "Gynecology"},
    {"doctor_id": "dr_037", "name": "Dr. David Park",       "email": "david.park@eyecare.com",          "specialty": "Ophthalmology"},
    {"doctor_id": "dr_038", "name": "Dr. Susan Taylor",     "email": "susan.taylor@eyecare.com",        "specialty": "Ophthalmology"},
    {"doctor_id": "dr_039", "name": "Dr. Mark Anderson",    "email": "mark.anderson@eyecare.com",       "specialty": "Ophthalmology"},
    {"doctor_id": "dr_040", "name": "Dr. Priya Sharma",     "email": "priya.sharma@skinclinic.com",     "specialty": "Dermatology"},
    {"doctor_id": "dr_041", "name": "Dr. Arun Nair",        "email": "arun.nair@skinclinic.com",        "specialty": "Dermatology"},
    {"doctor_id": "dr_042", "name": "Dr. Claire Bennett",   "email": "claire.bennett@skinclinic.com",   "specialty": "Dermatology"},
]


def _seed_doctor_accounts() -> None:
    with get_sessionmaker()() as db:
        repo = UserRepository(db)
        changed = False
        for seed in _DOCTOR_SEED:
            if repo.get_by_doctor_id(seed["doctor_id"]) is not None:
                continue
            repo.create_doctor(
                name=seed["name"],
                email=seed["email"].lower(),
                password_hash=_hash_password("Doctor123!"),
                doctor_id=seed["doctor_id"],
                specialty=seed["specialty"],
            )
            changed = True
            _log.info("Seeded doctor account: %s (%s)", seed["name"], seed["email"])
        if changed:
            db.commit()


def _load_report_from_disk(session_id: str) -> dict | None:
    # 1. Try DB first (works on Render where filesystem is ephemeral)
    db_report = _load_session_report_from_db(session_id)
    if db_report:
        return db_report

    # 2. Fall back to local disk (local dev)
    path = _repo_root / "logs" / "final_records" / f"final_{session_id}.json"
    if not path.exists():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        fp = rec.get("final_profile", {})
        report = {
            "schema_version": "0.7.0",
            "session_id": session_id,
            "patient": {
                "age": fp.get("demographics", {}).get("age"),
                "sex": fp.get("demographics", {}).get("sex"),
                "chief_complaint": fp.get("chief_complaint", ""),
            },
            "final_diagnosis": rec.get("primary_diagnosis", ""),
            "termination_reason": rec.get("termination_reason", ""),
            "ended_at": rec.get("ended_at", ""),
            "total_turns": len(rec.get("turn_history", [])),
            "aggregate": {},
            "turns": [],
            "closing_turn": rec.get("closing_turn"),
            "final_differential": rec.get("final_differential", []),
            "turn_history": [
                {
                    "turn_index": tr.get("turn_index", i),
                    "question": tr.get("doctor_output", {}).get("chosen_question", ""),
                    "rationale": tr.get("doctor_output", {}).get("rationale", ""),
                    "biggest_uncertainty": tr.get("doctor_output", {}).get("biggest_uncertainty", ""),
                    "patient_answer": tr.get("patient_answer", ""),
                    "differential": tr.get("doctor_output", {}).get("current_differential", []),
                    "confidence": tr.get("doctor_output", {}).get("confidence_to_stop", 0),
                    "doctor_output": tr.get("doctor_output", {}),
                }
                for i, tr in enumerate(rec.get("turn_history", []))
            ],
            "_from_disk": True,
        }
        # Cache to DB so future calls (after server restart) can find it
        _save_session_report(session_id, report)
        return report
    except Exception:
        return None


# Genuinely shared across multiple routers (appointments2.py,
# session_booking.py, sessions.py) — was accidentally deleted during an
# earlier Task 4 extraction (swept up along with a route removal that
# didn't account for a helper sitting between routes) and went undetected
# because lazy imports only fail at call time, not at route-registration
# time, and sampled smoke tests didn't happen to exercise the affected
# routes. Restored here from source read earlier in this session. See
# TASK4_SPLIT_ROUTERS.md for the static-check tooling added after this
# was caught, to prevent this class of bug going forward.
def _get_final_differential(session_id: str):
    """Return (differential, confidence) from live session or disk."""
    session = _sessions.get(session_id)
    if session is not None and session.complete and session._final_record is not None:
        diff = [(d.dx, d.prob) for d in session._final_record.final_differential]
        confidence = diff[0][1] if diff else 0.0
        return diff, confidence
    # Fall back to disk
    rec = _load_report_from_disk(session_id)
    if rec is None:
        return None, None
    raw_diff = rec.get("final_differential", [])
    # Normalise: may be list of dicts {"dx":..,"prob":..} or list of [dx, prob]
    diff = []
    for item in raw_diff:
        if isinstance(item, dict):
            diff.append((item["dx"], item["prob"]))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            diff.append((item[0], item[1]))
    confidence = diff[0][1] if diff else 0.0
    return diff, confidence


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# Still used directly by routes in this file that haven't moved to their
# own router yet — routers/doctors.py's require_role("doctor") dependency
# is the verified-equivalent replacement used by already-split routers.
def _require_doctor(request: Request):
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    if user.get("role") != "doctor":
        raise HTTPException(status_code=403, detail="Doctor account required.")
    return user


# ---------------------------------------------------------------------------
# Feature endpoints (Features 2,3,5,6,7,8,9)
# ---------------------------------------------------------------------------

# Feature 9 — Test Result Upload
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


# Loaded from disk so uploads survive server restarts between upload and booking
_session_test_uploads: dict = _load_session_uploads()  # session_id → { test_id → record }


# ---------------------------------------------------------------------------
# Feature: Patient messaging
# ---------------------------------------------------------------------------

def _load_waitlist() -> list:
    return _db_load("waitlist", [])


def _save_waitlist(entries: list) -> None:
    _db_save("waitlist", entries)


def _load_blocked_dates() -> dict:
    return _db_load("blocked_dates", {})


def _save_blocked_dates(data: dict) -> None:
    _db_save("blocked_dates", data)


# ---------------------------------------------------------------------------
# Feature: Email notifications helper (graceful no-op if unconfigured)
# ---------------------------------------------------------------------------

def _send_email_notification(to: str, subject: str, body: str) -> None:
    """Send an email via SMTP. Silently no-ops if SMTP_HOST env var is not set."""
    import smtplib, os as _os
    from email.mime.text import MIMEText
    host = _os.environ.get("SMTP_HOST", "")
    if not host or not to:
        return
    try:
        port = int(_os.environ.get("SMTP_PORT", "587"))
        user_env = _os.environ.get("SMTP_USER", "")
        pwd = _os.environ.get("SMTP_PASS", "")
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = _os.environ.get("SMTP_FROM", user_env)
        msg["To"] = to
        with smtplib.SMTP(host, port, timeout=5) as s:
            s.starttls()
            if user_env:
                s.login(user_env, pwd)
            s.sendmail(msg["From"], [to], msg.as_string())
    except Exception:
        pass  # never block the main flow


# ---------------------------------------------------------------------------
# Voice — ElevenLabs TTS proxy
# ---------------------------------------------------------------------------

_ELEVENLABS_VOICES = [
    {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel",  "description": "Calm, clear female — ideal for clinical explanations"},
    {"id": "ErXwobaYiN019PkySvjV", "name": "Antoni",  "description": "Warm, professional male"},
    {"id": "TxGEqnHWrfWFTfGW9XjX", "name": "Josh",    "description": "Deep, authoritative male"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "name": "Bella",   "description": "Soft, reassuring female"},
    {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam",    "description": "Neutral, clear male"},
    {"id": "AZnzlk1XvdvUeBnXmlld", "name": "Domi",    "description": "Strong, confident female"},
]


@app.get("/api/tts/voices")
async def list_tts_voices():
    """Return available ElevenLabs voice options."""
    return {"voices": _ELEVENLABS_VOICES}


@app.post("/api/tts")
async def text_to_speech(request: Request):
    """Proxy ElevenLabs TTS. Returns audio/mpeg stream."""
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ElevenLabs API key not configured. Set ELEVENLABS_API_KEY.")
    body = await request.json()
    text = (body.get("text") or "").strip()
    voice_id = body.get("voice_id") or "21m00Tcm4TlvDq8ikWAM"
    if not text:
        raise HTTPException(status_code=400, detail="text is required.")
    # Truncate to avoid large bills on very long messages
    if len(text) > 1000:
        text = text[:997] + "…"
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {"stability": 0.55, "similarity_boost": 0.75},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"ElevenLabs error: {r.status_code}")
        return Response(content=r.content, media_type="audio/mpeg",
                        headers={"Cache-Control": "no-store"})
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="TTS request timed out.")


# ---------------------------------------------------------------------------
# Static files — MUST come last (catch-all)
# ---------------------------------------------------------------------------

_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
