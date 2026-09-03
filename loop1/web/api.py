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
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

from loop1.schemas import Demographics, History, PatientProfile, Symptom
from loop3.routing.router import route as compute_routing
from web.api_session import APISession

from diffdx.routers.appointments import router as appointments_router
from diffdx.routers.appointments2 import router as appointments2_router
from diffdx.routers.appointments3 import router as appointments3_router
from diffdx.routers.appointments4 import router as appointments4_router
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

# Task 4 (split the monolith): routers peeled off one domain at a time.
# See TASK4_SPLIT_ROUTERS.md for what's moved and what's still here.
app.include_router(appointments_router)
app.include_router(appointments2_router)
app.include_router(appointments3_router)
app.include_router(appointments4_router)
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
# as part of Task 4's router split. Only the ones still used by routes in
# this file that haven't moved to their own router yet get imported back —
# trim this list further as more routers peel off (see TASK4_SPLIT_ROUTERS.md).
from diffdx.schemas.appointments import (
    BlockDateRequest,
    SecondOpinionRequest,
    SecondOpinionResponseRequest,
    TagsRequest,
    WaitlistRequest,
)


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
_TOKENS: dict[str, str] = {}       # token → user_id, in-memory; repopulated on startup
_USER_CACHE: dict[str, dict] = {}  # user_id → user data, invalidated on every _save_users

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


def _load_users() -> dict:
    return _db_load("users", {})


def _save_users(users: dict) -> None:
    _db_save("users", users)
    _USER_CACHE.clear()  # invalidate on every write


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


def _issue_token(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    _TOKENS[token] = user_id
    # Persist token in user record so it survives server restarts
    users = _load_users()
    if user_id in users:
        users[user_id].setdefault("tokens", []).append(token)
        _save_users(users)
    return token


def _get_user_from_request(request: Request) -> dict | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    user_id = _TOKENS.get(token)
    if not user_id:
        # Try loading from disk (after server restart)
        users = _load_users()
        for uid, u in users.items():
            if token in u.get("tokens", []):
                _TOKENS[token] = uid
                _USER_CACHE[uid] = u
                user_id = uid
                break
        if not user_id:
            return None
        return users.get(user_id)
    if user_id in _USER_CACHE:
        return _USER_CACHE[user_id]
    user = _load_users().get(user_id)
    if user:
        _USER_CACHE[user_id] = user
    return user


def _add_session_to_user(user_id: str, session_meta: dict) -> None:
    users = _load_users()
    if user_id not in users:
        return
    users[user_id].setdefault("sessions", []).append(session_meta)
    _save_users(users)


def _update_session_in_user(user_id: str, session_id: str, diagnosis: str, ended_at: str) -> None:
    users = _load_users()
    if user_id not in users:
        return
    for s in users[user_id].get("sessions", []):
        if s.get("session_id") == session_id:
            s["primary_diagnosis"] = diagnosis
            s["ended_at"] = ended_at
            s["status"] = "complete"
            break
    _save_users(users)


def _load_doctors() -> list:
    return _db_load("doctors", [])


def _save_doctors(doctors: list) -> None:
    _db_save("doctors", doctors)


def _load_appointments() -> dict:
    return _db_load("appointments", {})


def _save_appointments(appointments: dict) -> None:
    _db_save("appointments", appointments)


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
    users = _load_users()
    changed = False
    for seed in _DOCTOR_SEED:
        exists = any(
            u.get("role") == "doctor" and u.get("doctor_id") == seed["doctor_id"]
            for u in users.values()
        )
        if not exists:
            user_id = str(uuid.uuid4())
            users[user_id] = {
                "id": user_id,
                "name": seed["name"],
                "email": seed["email"].lower(),
                "password_hash": _hash_password("Doctor123!"),
                "role": "doctor",
                "doctor_id": seed["doctor_id"],
                "specialty": seed["specialty"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sessions": [],
                "tokens": [],
            }
            changed = True
            _log.info("Seeded doctor account: %s (%s)", seed["name"], seed["email"])
    if changed:
        _save_users(users)


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
# Feature: Symptom history across sessions (patient)
# ---------------------------------------------------------------------------

@app.get("/api/patient/symptom-history")
async def get_symptom_history(request: Request):
    """Return aggregated symptoms from all past sessions for the authenticated patient."""
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


def _load_second_opinions() -> list:
    return _db_load("second_opinions", [])


def _save_second_opinions(opinions: list) -> None:
    _db_save("second_opinions", opinions)


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
# Feature: Appointment Waitlist
# ---------------------------------------------------------------------------

@app.post("/api/patient/waitlist")
async def join_waitlist(req: WaitlistRequest, request: Request):
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    waitlist = _load_waitlist()
    # Check if already waiting for this doctor
    existing = next(
        (e for e in waitlist
         if e.get("patient_user_id") == user["id"]
         and e.get("doctor_id") == req.doctor_id
         and e.get("status") == "waiting"),
        None,
    )
    if existing:
        raise HTTPException(status_code=409, detail="Already on the waitlist for this doctor.")
    entry = {
        "id": str(uuid.uuid4()),
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
    return {"joined": True, "entry": entry}


@app.get("/api/patient/waitlist")
async def get_patient_waitlist(request: Request):
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    waitlist = _load_waitlist()
    mine = [e for e in waitlist if e.get("patient_user_id") == user["id"]]
    return {"waitlist": mine}


@app.delete("/api/patient/waitlist/{entry_id}")
async def leave_waitlist(entry_id: str, request: Request):
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    waitlist = _load_waitlist()
    new_list = [e for e in waitlist if not (e.get("id") == entry_id and e.get("patient_user_id") == user["id"])]
    if len(new_list) == len(waitlist):
        raise HTTPException(status_code=404, detail="Waitlist entry not found.")
    _save_waitlist(new_list)
    return {"removed": True}


@app.get("/api/doctor/waitlist")
async def get_doctor_waitlist(request: Request):
    doctor = _require_doctor(request)
    doctor_id = doctor.get("doctor_id")
    waitlist = _load_waitlist()
    mine = [e for e in waitlist if e.get("doctor_id") == doctor_id and e.get("status") == "waiting"]
    mine.sort(key=lambda e: e.get("joined_at", ""))
    return {"waitlist": mine, "count": len(mine)}


# ---------------------------------------------------------------------------
# Feature: Block Specific Dates
# ---------------------------------------------------------------------------

@app.post("/api/doctor/blocked-dates")
async def block_date(req: BlockDateRequest, request: Request):
    doctor = _require_doctor(request)
    doctor_id = doctor.get("doctor_id")
    data = _load_blocked_dates()
    if doctor_id not in data:
        data[doctor_id] = []
    # Remove any existing entry for this date
    data[doctor_id] = [d for d in data[doctor_id] if d.get("date") != req.date]
    data[doctor_id].append({"date": req.date, "reason": req.reason})
    data[doctor_id].sort(key=lambda d: d["date"])
    _save_blocked_dates(data)
    return {"blocked": True}


@app.get("/api/doctor/blocked-dates")
async def get_blocked_dates(request: Request):
    doctor = _require_doctor(request)
    doctor_id = doctor.get("doctor_id")
    data = _load_blocked_dates()
    return {"blocked_dates": data.get(doctor_id, [])}


@app.delete("/api/doctor/blocked-dates/{date}")
async def unblock_date(date: str, request: Request):
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
    return {"unblocked": True}


# ---------------------------------------------------------------------------
# Feature: Patient Tagging
# ---------------------------------------------------------------------------

@app.patch("/api/doctor/appointments/{appt_id}/tags")
async def update_patient_tags(appt_id: str, req: TagsRequest, request: Request):
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

import re as _re

def _parse_duration_days(duration_str: str) -> int | None:
    """Parse a duration string like '7 days', '2 weeks', '1 month' into days."""
    if not duration_str:
        return None
    m = _re.search(r'(\d+)\s*(day|week|month)', duration_str.lower())
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


@app.get("/api/doctor/renewal-reminders")
async def get_renewal_reminders(request: Request):
    """Return appointments where the patient has submitted a pending refill request."""
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


# ---------------------------------------------------------------------------
# Feature: Second Opinion Request
# ---------------------------------------------------------------------------

@app.post("/api/doctor/appointments/{appt_id}/second-opinion")
async def request_second_opinion(appt_id: str, req: SecondOpinionRequest, request: Request):
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

    # Build patient summary from appointment
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

    # Store on appointment
    appt["second_opinion"] = {
        "to_doctor_id": req.to_doctor_id,
        "to_doctor_name": req.to_doctor_name,
        "requested_at": now_str,
        "status": "pending",
        "opinion_id": opinion_id,
    }
    _save_appointments(appointments)

    # Email the receiving doctor
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


@app.get("/api/doctor/second-opinions/inbox")
async def get_second_opinion_inbox(request: Request):
    """Return second opinion requests sent TO this doctor."""
    doctor = _require_doctor(request)
    doctor_id = doctor.get("doctor_id")
    opinions = _load_second_opinions()
    inbox = [o for o in opinions if o.get("to_doctor_id") == doctor_id]
    inbox.sort(key=lambda o: o.get("requested_at", ""), reverse=True)
    return {"inbox": inbox, "count": len(inbox)}


@app.patch("/api/doctor/second-opinions/{opinion_id}/respond")
async def respond_to_second_opinion(opinion_id: str, req: SecondOpinionResponseRequest, request: Request):
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

    # Update appointment record
    appointments = _load_appointments()
    appt = appointments.get(opinion.get("appointment_id", ""))
    if appt and appt.get("second_opinion"):
        appt["second_opinion"]["status"] = "responded"
        appt["second_opinion"]["response"] = req.response
        _save_appointments(appointments)

    # Email the requesting doctor
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
