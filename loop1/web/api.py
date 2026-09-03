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

from diffdx.routers.auth import router as auth_router
from diffdx.routers.messaging import router as messaging_router
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
app.include_router(auth_router)
app.include_router(messaging_router)
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

class NotesRequest(BaseModel):
    notes: str


class StatusRequest(BaseModel):
    status: str  # "upcoming" | "seen" | "no_show"


class TestOrderItem(BaseModel):
    id: str
    test: str
    category: str       # blood | imaging | other
    priority: str       # routine | urgent | stat
    notes: str = ""
    ordered_at: str


class TestOrdersRequest(BaseModel):
    test_orders: list[TestOrderItem]


class ReferralRequest(BaseModel):
    specialty: str
    to_doctor: str = ""   # empty = any available specialist
    urgency: str = "Routine"
    notes: str = ""
    internal_note: str = ""   # doctor-only, never returned to patient endpoints
    referred_at: str = ""


class PlanItem(BaseModel):
    id: str
    text: str
    approved: bool = True
    source: str = "ai"   # "ai" | "doctor"


class ApprovedPlanRequest(BaseModel):
    plan: list[PlanItem]


class RescheduleRequest(BaseModel):
    slot: str


class SlotsRequest(BaseModel):
    slots: list[str]


class TestResultItem(BaseModel):
    test_id: str        # matches an existing test order id
    result: str
    status: str = "pending"   # pending | normal | abnormal | critical
    recorded_at: str


class TestResultsRequest(BaseModel):
    test_results: list[TestResultItem]


class PrescriptionItem(BaseModel):
    id: str
    drug: str
    dose: str
    route: str = "oral"
    frequency: str
    duration: str
    meal_timing: str = ""
    notes: str = ""
    prescribed_at: str


class PrescriptionsRequest(BaseModel):
    prescriptions: list[PrescriptionItem]
    force_new_batch: bool = False


class FollowUpRequest(BaseModel):
    slot: str
    notes: str = ""


class DoctorSummaryRequest(BaseModel):
    summary: str


class WaitlistRequest(BaseModel):
    doctor_id: str
    doctor_name: str
    specialty: str
    note: str = ""


class BlockDateRequest(BaseModel):
    date: str  # "YYYY-MM-DD"
    reason: str = ""


class TagsRequest(BaseModel):
    tags: list[str]


class SecondOpinionRequest(BaseModel):
    to_doctor_id: str
    to_doctor_name: str
    note: str = ""


class SecondOpinionResponseRequest(BaseModel):
    response: str


class ProposeRescheduleRequest(BaseModel):
    proposed_slot: str
    reason: str = ""


class RescheduleResponseRequest(BaseModel):
    action: str  # 'accept' or 'decline'


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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# HTML page routes — serve clean URLs without .html extension
_html = lambda name: FileResponse(
    _static_dir / name,
    media_type="text/html",
    headers={"Cache-Control": "no-store"},
)

@app.get("/patient-info")
async def page_patient_info(): return _html("patient-info.html")

@app.get("/history")
async def page_history(): return _html("history.html")

@app.get("/login")
async def page_login(): return _html("login.html")

@app.get("/doctor-portal")
async def page_doctor_portal(): return _html("doctor-portal.html")

@app.get("/doctor-portal.html")
async def page_doctor_portal_html(): return _html("doctor-portal.html")

@app.get("/session/{session_id}")
async def page_session(session_id: str): return _html("session.html")

@app.get("/report/{session_id}")
async def page_report(session_id: str): return _html("report.html")


@app.get("/api/appointments")
async def get_patient_appointments(request: Request):
    """Return all booked appointments for the authenticated patient."""
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    appointments = _load_appointments()
    patient_appts = []
    for a in appointments.values():
        if a.get("patient_user_id") != user["id"]:
            continue
        entry = dict(a)
        # Strip doctor-only fields before sending to patient
        if "referral" in entry and "internal_note" in entry["referral"]:
            entry["referral"] = {k: v for k, v in entry["referral"].items() if k != "internal_note"}
        patient_appts.append(entry)
    patient_appts.sort(key=lambda a: a.get("slot", ""), reverse=True)
    return {"appointments": patient_appts}


@app.get("/api/patient/test-notifications")
async def get_test_notifications(request: Request):
    """Return pending test order count for the logged-in patient (used for nav badge)."""
    user = _get_user_from_request(request)
    if not user:
        return {"count": 0, "appointments": []}
    appointments = _load_appointments()
    pending = [
        {"appt_id": a.get("id"), "doctor_name": a.get("doctor_name"), "slot": a.get("slot"),
         "session_id": a.get("session_id"), "test_count": len(a.get("test_orders", [])),
         "has_stat": any(t.get("priority") == "stat" for t in a.get("test_orders", []))}
        for a in appointments.values()
        if a.get("patient_user_id") == user["id"] and a.get("test_orders")
    ]
    return {"count": sum(p["test_count"] for p in pending), "appointments": pending}


# ---------------------------------------------------------------------------
# Doctor portal endpoints
# ---------------------------------------------------------------------------

def _require_doctor(request: Request):
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    if user.get("role") != "doctor":
        raise HTTPException(status_code=403, detail="Doctor account required.")
    return user


@app.get("/api/doctor/appointments")
async def get_doctor_appointments(request: Request):
    """List all appointments assigned to this doctor."""
    doctor = _require_doctor(request)
    doctor_id = doctor.get("doctor_id")
    appointments = _load_appointments()
    mine = [a for a in appointments.values() if a.get("doctor_id") == doctor_id]
    mine.sort(key=lambda a: a.get("slot", ""))
    return mine


@app.patch("/api/doctor/appointments/{appt_id}/tests")
async def update_test_orders(appt_id: str, req: TestOrdersRequest, request: Request):
    """Save the doctor's test orders for an appointment."""
    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["test_orders"] = [t.model_dump() for t in req.test_orders]
    appt["tests_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)
    return {"saved": True, "count": len(req.test_orders)}


@app.post("/api/doctor/appointments/{appt_id}/files")
async def doctor_upload_file(
    appt_id: str, request: Request,
    file: UploadFile = File(...),
    test_order_id: str | None = None,
):
    """Doctor uploads a result file for an appointment (e.g. lab report PDF)."""
    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    _ALLOWED = {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp", "image/heic"}
    ct = (file.content_type or "").split(";")[0].strip().lower()
    if ct not in _ALLOWED:
        raise HTTPException(status_code=415, detail="Only PDF and image files are allowed.")
    raw = await file.read()
    if len(raw) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB).")
    data_b64 = base64.b64encode(raw).decode("ascii")
    _save_file_data(appt_id, file.filename, data_b64)
    record = {
        "filename": file.filename,
        "size_bytes": len(raw),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "mime_type": file.content_type or "application/octet-stream",
        "uploaded_by": "doctor",
        "test_order_id": test_order_id or None,
    }
    files = appt.setdefault("patient_files", [])
    files[:] = [f for f in files if f.get("filename") != file.filename]
    files.append(record)
    if test_order_id:
        for t in appt.get("test_orders", []):
            if t.get("id") == test_order_id:
                t["results_uploaded"] = True
                t["results_filename"] = file.filename
                break
    _save_appointments(appointments)
    return {"saved": True, "filename": file.filename, "size_bytes": len(raw)}


@app.post("/api/doctor/appointments/{appt_id}/referral")
async def save_referral(appt_id: str, req: ReferralRequest, request: Request):
    """Save a referral issued by the doctor for this appointment."""
    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["referral"] = {
        "specialty": req.specialty,
        "to_doctor": req.to_doctor,
        "urgency": req.urgency,
        "notes": req.notes,
        "internal_note": req.internal_note,   # stored but stripped from patient-facing GET /api/appointments
        "referred_at": req.referred_at or datetime.now(timezone.utc).isoformat(),
        "referring_doctor": doctor.get("name", ""),
    }
    _save_appointments(appointments)
    return {"saved": True}


@app.patch("/api/doctor/appointments/{appt_id}/notes")
async def update_doctor_notes(appt_id: str, req: NotesRequest, request: Request):
    """Save doctor's free-text notes on an appointment."""
    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["doctor_notes"] = req.notes
    appt["notes_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)
    return {"saved": True}


@app.patch("/api/doctor/appointments/{appt_id}/status")
async def update_appointment_status(appt_id: str, req: StatusRequest, request: Request):
    """Update appointment status: upcoming | seen | no_show."""
    doctor = _require_doctor(request)
    valid = {"upcoming", "seen", "no_show"}
    if req.status not in valid:
        raise HTTPException(status_code=400, detail=f"Status must be one of: {valid}")
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    old_status = appt.get("status", "upcoming")
    appt["status"] = req.status
    appt["status_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)

    # Waitlist notification: when appointment is cancelled, notify first waiting patient
    if req.status == "cancelled" and old_status != "cancelled":
        doctor_id = appt.get("doctor_id", "")
        if doctor_id:
            waitlist = _load_waitlist()
            waiting = [e for e in waitlist if e.get("doctor_id") == doctor_id and e.get("status") == "waiting"]
            if waiting:
                first = waiting[0]
                first["status"] = "notified"
                _save_waitlist(waitlist)
                # Email the waiting patient
                users = _load_users()
                patient = users.get(first.get("patient_user_id", ""), {})
                p_email = patient.get("email", "")
                p_name = patient.get("name", "Patient")
                _send_email_notification(
                    to=p_email,
                    subject=f"Slot Available — {first.get('doctor_name', 'Your doctor')}",
                    body=(
                        f"Hi {p_name},\n\n"
                        f"Good news! A slot has opened up with {first.get('doctor_name', 'your doctor')} ({first.get('specialty', '')}).\n"
                        f"Please log in to DiffDx to book your appointment before it fills up.\n"
                    ),
                )

    return {"status": req.status}


@app.get("/api/doctors")
async def list_all_doctors():
    """Return all doctors (name, specialty, hospital, rating, avatar_initials, available_slots) for patient search."""
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


@app.get("/api/doctors/{doctor_id}/slots")
async def get_doctor_slots(doctor_id: str, request: Request):
    """Return available slots for a doctor (for rescheduling)."""
    _require_doctor(request)
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == doctor_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doctor not found.")
    return {"slots": doc.get("available_slots", [])}


@app.patch("/api/doctor/appointments/{appt_id}/reschedule")
async def reschedule_appointment(appt_id: str, req: RescheduleRequest, request: Request):
    """Reschedule an appointment to a new slot."""
    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == appt["doctor_id"]), None)
    if doc is None or req.slot not in doc.get("available_slots", []):
        raise HTTPException(status_code=400, detail="Slot not available.")
    blocked_data = _load_blocked_dates()
    blocked_dates = {d["date"] for d in blocked_data.get(appt["doctor_id"], [])}
    if req.slot[:10] in blocked_dates:
        raise HTTPException(status_code=400, detail="That date is blocked on the doctor's calendar.")
    if appt.get("reschedule_proposal", {}).get("status") == "pending":
        raise HTTPException(status_code=409, detail="A reschedule proposal is pending patient response. Cancel or wait for it to resolve first.")
    old_slot = appt.get("slot")
    appt["slot"] = req.slot
    appt["rescheduled_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)
    # Remove the new slot from available; add the old one back
    slots = set(doc.get("available_slots", []))
    slots.discard(req.slot)
    if old_slot:
        slots.add(old_slot)
    doc["available_slots"] = sorted(slots)
    _save_doctors(doctors)
    return {"slot": req.slot}


@app.post("/api/doctor/appointments/{appt_id}/propose-reschedule")
async def propose_reschedule(appt_id: str, req: ProposeRescheduleRequest, request: Request):
    """Doctor proposes a new slot to the patient; patient must accept or decline."""
    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    blocked_data = _load_blocked_dates()
    blocked_dates = {d["date"] for d in blocked_data.get(doctor.get("doctor_id", ""), [])}
    if req.proposed_slot[:10] in blocked_dates:
        raise HTTPException(status_code=400, detail="That date is blocked on your calendar.")
    appt["reschedule_proposal"] = {
        "proposed_slot": req.proposed_slot,
        "reason": req.reason,
        "status": "pending",
        "proposed_at": datetime.now(timezone.utc).isoformat(),
        "proposed_by": doctor.get("name", "Your doctor"),
    }
    _save_appointments(appointments)
    # Notify patient by email
    users = _load_users()
    patient = next((u for u in users.values() if u.get("id") == appt.get("patient_user_id")), None)
    if patient:
        old_slot = (appt.get("slot") or "").replace("T", " at ").replace(":00", "")
        new_slot = req.proposed_slot.replace("T", " at ").replace(":00", "")
        _send_email_notification(
            to=patient.get("email", ""),
            subject="Appointment Reschedule Proposed",
            body=f"Hi {patient.get('name','')},\n\n{doctor.get('name','Your doctor')} has proposed to reschedule your appointment.\n\nCurrent slot: {old_slot}\nProposed slot: {new_slot}\nReason: {req.reason or 'Date unavailable'}\n\nPlease log in and accept the new slot or book with another doctor.",
        )
    return {"status": "proposed"}


@app.patch("/api/patient/appointments/{appt_id}/reschedule-response")
async def patient_reschedule_response(appt_id: str, req: RescheduleResponseRequest, request: Request):
    """Patient accepts or declines a doctor's reschedule proposal."""
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("patient_user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    proposal = appt.get("reschedule_proposal")
    if not proposal or proposal.get("status") != "pending":
        raise HTTPException(status_code=400, detail="No pending reschedule proposal.")
    if req.action == "accept":
        old_slot = appt.get("slot")
        new_slot = proposal["proposed_slot"]
        appt["slot"] = new_slot
        appt["rescheduled_at"] = datetime.now(timezone.utc).isoformat()
        appt["reschedule_proposal"]["status"] = "accepted"
        # Swap slots on doctor record
        doctors = _load_doctors()
        doc = next((d for d in doctors if d["id"] == appt.get("doctor_id")), None)
        if doc:
            slots = set(doc.get("available_slots", []))
            slots.discard(new_slot)
            if old_slot:
                slots.add(old_slot)
            doc["available_slots"] = sorted(slots)
            _save_doctors(doctors)
    elif req.action == "decline":
        appt["reschedule_proposal"]["status"] = "declined"
    else:
        raise HTTPException(status_code=400, detail="action must be accept or decline.")
    _save_appointments(appointments)
    # Notify doctor
    users = _load_users()
    doctor_user = next((u for u in users.values() if u.get("doctor_id") == appt.get("doctor_id")), None)
    if doctor_user:
        action_label = "accepted" if req.action == "accept" else "declined"
        _send_email_notification(
            to=doctor_user.get("email", ""),
            subject=f"Reschedule {action_label.capitalize()} by Patient",
            body=f"Hi {doctor_user.get('name','')},\n\n{appt.get('patient_name','Your patient')} has {action_label} the reschedule proposal for their appointment.",
        )
    return {"status": req.action}


@app.patch("/api/doctor/appointments/{appt_id}/plan")
async def update_approved_plan(appt_id: str, req: ApprovedPlanRequest, request: Request):
    """Save the doctor's approved / modified AI plan for an appointment."""
    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["approved_plan"] = [p.model_dump() for p in req.plan]
    appt["plan_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)
    return {"saved": True, "count": len(req.plan)}


@app.patch("/api/doctor/appointments/{appt_id}/test-results")
async def update_test_results(appt_id: str, req: TestResultsRequest, request: Request):
    """Save lab results against test orders for an appointment."""
    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["test_results_data"] = [r.model_dump() for r in req.test_results]
    appt["results_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)
    return {"saved": True, "count": len(req.test_results)}


@app.get("/api/doctor/appointments/{appt_id}/file")
async def doctor_download_patient_file(appt_id: str, request: Request, filename: str = ""):
    """Serve a patient-uploaded file to the doctor assigned to that appointment.
    Filename is passed as a query param (?filename=...) to avoid Starlette path-decode
    issues with non-ASCII characters (e.g. macOS screenshot narrow no-break space U+202F)."""
    doctor = _require_doctor(request)
    if not filename:
        raise HTTPException(status_code=400, detail="filename query param required.")
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    rec = next((f for f in appt.get("patient_files", []) if f.get("filename") == filename), None)
    if rec is None:
        raise HTTPException(status_code=404, detail="File not found.")
    raw_b64 = (
        _load_file_data(appt_id, filename)
        or _load_file_data(f"session:{appt.get('session_id', '')}", filename)
        or rec.get("data_b64", "")
    )
    if not raw_b64:
        raise HTTPException(status_code=404, detail="File data not found.")
    data = base64.b64decode(raw_b64)
    safe_name = filename.encode("ascii", "replace").decode("ascii")
    return Response(
        content=data,
        media_type=rec.get("mime_type", "application/octet-stream"),
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


@app.get("/api/doctor/patient-history/{patient_name}")
async def get_patient_history(patient_name: str, request: Request, exclude: str = ""):
    """Return all appointments for a patient (case-insensitive), sorted newest first."""
    _require_doctor(request)
    appointments = _load_appointments()
    history = [
        a for a in appointments.values()
        if a.get("patient_name", "").lower() == patient_name.lower()
        and a.get("appointment_id") != exclude
    ]
    history.sort(key=lambda a: a.get("slot", ""), reverse=True)
    return {"history": history}


@app.patch("/api/doctor/appointments/{appt_id}/prescriptions")
async def update_prescriptions(appt_id: str, req: PrescriptionsRequest, request: Request):
    """Save prescription pad for an appointment."""
    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    now_iso = datetime.now(timezone.utc).isoformat()
    new_rx = [p.model_dump() for p in req.prescriptions]
    # Maintain history: update last batch in-place if saved < 10 min ago AND same IDs, else append
    history = appt.get("prescription_history", [])
    def _rx_ids(batch): return {p.get("id") for p in batch.get("prescriptions", [])}
    new_ids = {p.id for p in req.prescriptions}
    if not req.force_new_batch and history:
        last_saved = history[-1].get("saved_at", "")
        try:
            age_s = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(last_saved)).total_seconds()
        except Exception:
            age_s = 999
        same_items = _rx_ids(history[-1]) == new_ids
        if age_s < 600 and same_items:
            history[-1]["prescriptions"] = new_rx
            history[-1]["saved_at"] = now_iso
        else:
            history.append({"saved_at": now_iso, "prescriptions": new_rx})
    else:
        history.append({"saved_at": now_iso, "prescriptions": new_rx})
    appt["prescription_history"] = history
    appt["prescriptions"] = new_rx  # keep for backward compat
    appt["prescriptions_updated_at"] = now_iso
    _save_appointments(appointments)
    return {"saved": True, "count": len(new_rx)}


@app.post("/api/doctor/appointments/{appt_id}/followup")
async def create_followup(appt_id: str, req: FollowUpRequest, request: Request):
    """Create a follow-up appointment linked to an existing appointment."""
    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    # Validate slot
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == appt.get("doctor_id")), None)
    if doc is None or req.slot not in doc.get("available_slots", []):
        raise HTTPException(status_code=400, detail="Slot not available.")
    # Remove slot from doctor's available slots
    doc["available_slots"] = [s for s in doc["available_slots"] if s != req.slot]
    _save_doctors(doctors)
    # Create follow-up appointment record
    followup_id = str(uuid.uuid4())
    followup = {
        "appointment_id": followup_id,
        "session_id": "",
        "patient_user_id": appt.get("patient_user_id", ""),
        "patient_name": appt.get("patient_name", ""),
        "doctor_id": appt.get("doctor_id"),
        "doctor_name": appt.get("doctor_name", ""),
        "specialty": appt.get("specialty", ""),
        "slot": req.slot,
        "booked_at": datetime.now(timezone.utc).isoformat(),
        "primary_diagnosis": appt.get("primary_diagnosis", ""),
        "urgency": appt.get("urgency", "routine"),
        "status": "upcoming",
        "is_followup": True,
        "parent_appointment_id": appt_id,
        "followup_notes": req.notes,
    }
    appointments[followup_id] = followup
    _save_appointments(appointments)
    return {"followup_appointment_id": followup_id, "slot": req.slot}


@app.patch("/api/doctor/appointments/{appt_id}/summary")
async def update_doctor_summary(appt_id: str, req: DoctorSummaryRequest, request: Request):
    """Save a doctor's plain-language summary for the patient."""
    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    appt["doctor_summary"] = req.summary
    appt["summary_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_appointments(appointments)
    return {"saved": True}


@app.get("/api/doctor/appointments/{appt_id}")
async def get_doctor_appointment_detail(appt_id: str, request: Request):
    """Full appointment detail: patient info + AI session data.
    Any authenticated doctor may view; only the owning doctor may edit."""
    _require_doctor(request)

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")

    session_id = appt["session_id"]

    # Load session report
    session = _sessions.get(session_id)
    if session and session.complete:
        report = session.get_report()
    else:
        report = _load_report_from_disk(session_id)

    # Load routing
    diff, confidence = _get_final_differential(session_id)
    routing_data = None
    if diff:
        rd = compute_routing(session_id, diff, confidence)
        routing_data = {
            "specialty": rd.primary_routing.specialty,
            "urgency": rd.primary_routing.urgency.value,
            "appointment_type": rd.primary_routing.appointment_type.value,
            "reasoning": rd.primary_routing.reasoning,
            "is_emergency": rd.primary_routing.urgency.value == "emergency",
            "is_ambiguous": rd.is_ambiguous,
            "final_confidence": rd.final_confidence,
        }

    # Load full turn history — prefer DB report (works on Render), fall back to disk
    turn_history = []
    if report and report.get("turn_history"):
        turn_history = report["turn_history"]
    else:
        jsonl_path = _repo_root / "logs" / f"session_{session_id}.jsonl"
        if jsonl_path.exists():
            try:
                lines = jsonl_path.read_text(encoding="utf-8").splitlines()
                for line in lines:
                    if line.strip():
                        turn_history.append(json.loads(line))
            except Exception:
                pass

    # Include doctor's available slots so the frontend can open a reschedule calendar
    # without needing a separate API call (avoids StaticFiles catch-all conflicts)
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == appt.get("doctor_id")), None)
    now_prefix = datetime.now(timezone.utc).isoformat()[:16]
    doctor_slots = [s for s in doc.get("available_slots", []) if s >= now_prefix] if doc else []

    return {
        "appointment": appt,
        "session_report": report,
        "routing": routing_data,
        "turn_history": turn_history,
        "doctor_slots": doctor_slots,
    }


@app.get("/api/doctor/profile")
async def get_doctor_profile(request: Request):
    """Return the authenticated doctor's profile including available_slots."""
    doctor_user = _require_doctor(request)
    doctor_id = doctor_user.get("doctor_id")
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == doctor_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doctor record not found.")
    return {"doctor": doc}


@app.patch("/api/doctor/profile-details")
async def update_doctor_profile_details(request: Request):
    """Save professional, education, and bio details for the authenticated doctor."""
    doctor_user = _require_doctor(request)
    doctor_id   = doctor_user.get("doctor_id")
    body = await request.json()

    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == doctor_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doctor record not found.")

    # Top-level fields mirrored to doc root for easy display elsewhere
    if "specialty" in body:
        doc["specialty"] = body["specialty"]
    if "hospital" in body:
        doc["hospital"] = body["hospital"]

    # Nested profile block
    prof = doc.setdefault("profile", {})
    for field in ("mobile", "experience", "license", "fee", "bio"):
        if field in body:
            prof[field] = body[field]
    if "education" in body:
        prof["education"] = body["education"]

    _save_doctors(doctors)

    # Also update the display name in users.json if it changed
    if "name" in body and body["name"]:
        users = _load_users()
        for uid, u in users.items():
            if u.get("doctor_id") == doctor_id:
                u["name"] = body["name"]
                break
        _save_users(users)

    return {"saved": True}


@app.patch("/api/doctor/slots")
async def update_doctor_slots(req: SlotsRequest, request: Request):
    """Replace the authenticated doctor's available_slots list."""
    doctor_user = _require_doctor(request)
    doctor_id = doctor_user.get("doctor_id")
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == doctor_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doctor record not found.")
    doc["available_slots"] = sorted(set(req.slots))
    _save_doctors(doctors)
    return {"saved": True, "count": len(doc["available_slots"])}


# ---------------------------------------------------------------------------
# Feature endpoints (Features 2,3,5,6,7,8,9)
# ---------------------------------------------------------------------------

class ScheduleRange(BaseModel):
    start: str   # "HH:MM"
    end: str     # "HH:MM"


class ScheduleTemplateRequest(BaseModel):
    template: dict[str, list[ScheduleRange]]   # {"mon":[{start,end}], ...}
    weeks: int = 4


class IntakeRequest(BaseModel):
    feeling: str = ""
    symptoms: list[str] = []
    severity: int = 5
    changes: str = ""
    medications: list[str] = []
    allergies: str = ""
    tests_done: list[str] = []


class RefillRequest(BaseModel):
    medications: list[dict] = []
    note: str = ""


class DirectBookRequest(BaseModel):
    doctor_id: str
    slot: str
    note: str = ""
    dependent_id: str | None = None
    patient_name_override: str | None = None


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


@app.post("/api/doctor/schedule-template/apply")
async def apply_schedule_template(req: ScheduleTemplateRequest, request: Request):
    """Generate ISO slots for the next N weeks from a weekly template and merge into available_slots."""
    doctor_user = _require_doctor(request)
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
def _iso_week_label(dt: datetime) -> tuple:
    iso = dt.isocalendar()
    return (iso[0], iso[1])


@app.get("/api/doctor/analytics")
async def get_doctor_analytics(request: Request):
    """Compute KPI / utilization / no-show analytics from the doctor's appointments."""
    doctor_user = _require_doctor(request)
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
@app.get("/api/patient/history")
async def get_patient_history_timeline(request: Request):
    """Return the patient's own appointments newest-first, with session_id where available."""
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
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


def _patient_appt_or_403(appt_id: str, request: Request) -> tuple[dict, dict]:
    """Return (appointments_dict, appt) verifying the appointment belongs to the caller."""
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("patient_user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not your appointment.")
    return appointments, appt


# Feature 6 — Pre-visit Symptom Intake
@app.post("/api/patient/appointments/{appt_id}/intake")
async def save_intake(appt_id: str, req: IntakeRequest, request: Request):
    appointments, appt = _patient_appt_or_403(appt_id, request)
    appt["intake"] = {
        **req.model_dump(),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_appointments(appointments)
    return {"saved": True}


# Feature 7 — Prescription Refill Request
@app.post("/api/patient/appointments/{appt_id}/refill")
async def request_refill(appt_id: str, req: RefillRequest, request: Request):
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


@app.patch("/api/doctor/appointments/{appt_id}/refill")
async def fulfill_refill(appt_id: str, request: Request):
    """Doctor marks a refill request as given/fulfilled."""
    doctor_user = _get_user_from_request(request)
    if not doctor_user or doctor_user.get("role") != "doctor":
        raise HTTPException(status_code=403, detail="Doctor access required.")
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
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


@app.get("/api/doctor/pending-refills")
async def get_pending_refills(request: Request):
    """Return count of appointments with pending refill requests for the logged-in doctor."""
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
@app.post("/api/patient/book-direct")
async def book_direct(req: DirectBookRequest, request: Request):
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
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


# Feature 9 — Test Result Upload
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


# Loaded from disk so uploads survive server restarts between upload and booking
_session_test_uploads: dict = _load_session_uploads()  # session_id → { test_id → record }

@app.post("/api/session/{session_id}/suggested-test-files")
async def upload_suggested_test_file(
    session_id: str, request: Request,
    file: UploadFile = File(...),
    suggested_test_id: str | None = None,
    suggested_test_name: str | None = None,
):
    """Upload a result file for an AI-suggested test. Works with or without a booked appointment."""
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    _ALLOWED = {"application/pdf","image/jpeg","image/png","image/gif","image/webp","image/heic"}
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED:
        raise HTTPException(status_code=415, detail="Only PDF and image files are allowed.")
    raw = await file.read()
    if len(raw) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB).")

    data_b64 = base64.b64encode(raw).decode("ascii")
    record = {
        "filename": file.filename,
        "size_bytes": len(raw),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "mime_type": file.content_type or "application/octet-stream",
        "suggested_test_id": suggested_test_id or None,
        "suggested_test_name": suggested_test_name or None,
        "session_id": session_id,
        "user_id": user["id"],
    }

    # Store binary data separately (not inline in appointments blob)
    _save_file_data(f"session:{session_id}", file.filename, data_b64)

    # Store metadata-only record in session uploads bucket
    bucket = _session_test_uploads.setdefault(session_id, {})
    tid = suggested_test_id or file.filename
    bucket[tid] = record
    _save_session_uploads(_session_test_uploads)

    # Also attach to matching appointment if one exists
    appointments = _load_appointments()
    for appt in appointments.values():
        if appt.get("session_id") == session_id and appt.get("patient_user_id") == user["id"]:
            appt_id = appt["appointment_id"]
            _save_file_data(appt_id, file.filename, data_b64)
            files = appt.setdefault("patient_files", [])
            files[:] = [f for f in files if f.get("filename") != file.filename]
            files.append(record)
            if suggested_test_id:
                appt.setdefault("suggested_test_uploads", {})[suggested_test_id] = {
                    "filename": file.filename,
                    "uploaded_at": record["uploaded_at"],
                    "test_name": suggested_test_name or suggested_test_id,
                }
            _save_appointments(appointments)
            break

    return {"saved": True, "filename": file.filename, "size_bytes": len(raw)}


@app.get("/api/session/{session_id}/suggested-test-files")
async def list_suggested_test_files(session_id: str, request: Request):
    """Return already-uploaded suggested test files for this session."""
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    bucket = _session_test_uploads.get(session_id, {})
    return {
        "uploads": {
            tid: {k: v for k, v in rec.items() if k != "data_b64"}
            for tid, rec in bucket.items()
            if rec.get("user_id") == user["id"]
        }
    }


@app.post("/api/patient/appointments/{appt_id}/files")
async def upload_patient_file(
    appt_id: str, request: Request,
    file: UploadFile = File(...),
    test_order_id: str | None = None,       # ties file to a doctor-ordered test
    suggested_test_id: str | None = None,   # ties file to an AI-suggested test
    suggested_test_name: str | None = None, # human label for the suggested test
):
    appointments, appt = _patient_appt_or_403(appt_id, request)
    _ALLOWED_UPLOAD_TYPES = {
        "application/pdf", "image/jpeg", "image/png",
        "image/gif", "image/webp", "image/heic",
    }
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_UPLOAD_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Only PDF and image files (JPEG, PNG, GIF, WebP, HEIC) are allowed.",
        )
    raw = await file.read()
    if len(raw) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB).")
    data_b64 = base64.b64encode(raw).decode("ascii")
    # Store file bytes separately so appointments JSON stays small
    _save_file_data(appt_id, file.filename, data_b64)
    record = {
        "filename": file.filename,
        "size_bytes": len(raw),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "mime_type": file.content_type or "application/octet-stream",
        "test_order_id": test_order_id or None,
        "suggested_test_id": suggested_test_id or None,
        "suggested_test_name": suggested_test_name or None,
    }
    files = appt.setdefault("patient_files", [])
    # Replace any existing file with the same name
    files[:] = [f for f in files if f.get("filename") != file.filename]
    files.append(record)
    # Mark the matched doctor-ordered test as having results uploaded
    if test_order_id:
        for t in appt.get("test_orders", []):
            if t.get("id") == test_order_id:
                t["results_uploaded"] = True
                t["results_filename"] = file.filename
                break
    # Mark the matched suggested test as uploaded
    if suggested_test_id:
        sug_uploads = appt.setdefault("suggested_test_uploads", {})
        sug_uploads[suggested_test_id] = {
            "filename": file.filename,
            "uploaded_at": record["uploaded_at"],
            "test_name": suggested_test_name or suggested_test_id,
        }
    _save_appointments(appointments)
    return {"saved": True, "filename": file.filename, "size_bytes": len(raw)}


@app.get("/api/patient/appointments/{appt_id}/files")
async def list_patient_files(appt_id: str, request: Request):
    _appointments, appt = _patient_appt_or_403(appt_id, request)
    files = [
        {k: v for k, v in f.items() if k != "data_b64"}
        for f in appt.get("patient_files", [])
    ]
    return {"files": files}


@app.delete("/api/patient/appointments/{appt_id}/files/{filename}")
async def delete_patient_file(appt_id: str, filename: str, request: Request):
    appointments, appt = _patient_appt_or_403(appt_id, request)
    files = appt.get("patient_files", [])
    new_files = [f for f in files if f.get("filename") != filename]
    if len(new_files) == len(files):
        raise HTTPException(status_code=404, detail="File not found.")
    appt["patient_files"] = new_files
    _save_appointments(appointments)
    return {"deleted": True}


@app.get("/api/patient/appointments/{appt_id}/files/{filename}")
async def download_patient_file(appt_id: str, filename: str, request: Request):
    """Download a patient file. Auth accepted via Bearer header or ?token= query for direct links."""
    user = _get_user_from_request(request)
    if not user:
        token = request.query_params.get("token", "")
        if token:
            user_id = _TOKENS.get(token)
            if not user_id:
                users = _load_users()
                for uid, u in users.items():
                    if token in u.get("tokens", []):
                        _TOKENS[token] = uid
                        user_id = uid
                        break
            if user_id:
                user = _load_users().get(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    # Allow both the owning patient and the assigned doctor to download
    is_owner = appt.get("patient_user_id") == user["id"]
    is_doctor = user.get("role") == "doctor" and appt.get("doctor_id") == user.get("doctor_id")
    if not (is_owner or is_doctor):
        raise HTTPException(status_code=403, detail="Not permitted.")

    rec = next((f for f in appt.get("patient_files", []) if f.get("filename") == filename), None)
    if rec is None:
        raise HTTPException(status_code=404, detail="File not found.")
    raw_b64 = (
        _load_file_data(appt_id, filename)
        or _load_file_data(f"session:{appt.get('session_id', '')}", filename)
        or rec.get("data_b64", "")
    )
    if not raw_b64:
        raise HTTPException(status_code=404, detail="File data not found.")
    data = base64.b64decode(raw_b64)
    return Response(
        content=data,
        media_type=rec.get("mime_type", "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Feature: Appointment cancellation (patient)
# ---------------------------------------------------------------------------

@app.delete("/api/patient/appointments/{appt_id}")
async def cancel_patient_appointment(appt_id: str, request: Request):
    """Patient cancels their own upcoming appointment."""
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("patient_user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not your appointment.")
    if appt.get("status") not in ("upcoming", None):
        raise HTTPException(status_code=400, detail="Only upcoming appointments can be cancelled.")
    appt["status"] = "cancelled"
    appt["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    appt["cancelled_by"] = "patient"
    _save_appointments(appointments)
    # Return the slot to the doctor's available pool
    freed_slot = appt.get("slot", "")
    if freed_slot:
        doctors = _load_doctors()
        doc = next((d for d in doctors if d["id"] == appt.get("doctor_id")), None)
        if doc is not None:
            slots = set(doc.get("available_slots", []))
            slots.add(freed_slot)
            doc["available_slots"] = sorted(slots)
            _save_doctors(doctors)
    _send_email_notification(
        to=user.get("email", ""),
        subject="Appointment Cancelled",
        body=f"Your appointment with {appt.get('doctor_name','your doctor')} on {appt.get('slot','')} has been cancelled.",
    )
    return {"cancelled": True, "freed_slot": freed_slot}


@app.delete("/api/patient/appointments/{appt_id}/dismiss")
async def patient_dismiss_appointment(appt_id: str, request: Request):
    """Permanently remove a cancelled or missed appointment from the patient's view."""
    user = _get_user_from_request(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("patient_user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    now_iso = datetime.now(timezone.utc).isoformat()[:16]
    status = appt.get("status", "")
    slot = appt.get("slot", "")
    is_missed = status == "upcoming" and slot < now_iso
    if status != "cancelled" and not is_missed:
        raise HTTPException(status_code=400, detail="Only cancelled or missed appointments can be deleted.")
    del appointments[appt_id]
    _save_appointments(appointments)
    return {"dismissed": True}


@app.delete("/api/doctor/appointments/{appt_id}/dismiss")
async def doctor_dismiss_appointment(appt_id: str, request: Request):
    """Permanently remove a cancelled appointment from the doctor's list."""
    doctor = _require_doctor(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("doctor_id") != doctor.get("doctor_id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    if appt.get("status") != "cancelled":
        raise HTTPException(status_code=400, detail="Only cancelled appointments can be removed.")
    del appointments[appt_id]
    _save_appointments(appointments)
    return {"dismissed": True}


@app.get("/api/patient/doctors/{doctor_id}/slots")
async def patient_get_doctor_slots(doctor_id: str, request: Request):
    """Return available slots for a doctor — patient-facing, no doctor auth."""
    _get_user_from_request(request)
    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == doctor_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doctor not found.")
    now_iso = datetime.now(timezone.utc).isoformat()
    available = [s for s in doc.get("available_slots", []) if s >= now_iso]
    return {"slots": sorted(available), "doctor_name": doc.get("name", "")}


class PatientRescheduleRequest(BaseModel):
    new_slot: str
    note: str = ""


@app.post("/api/patient/appointments/{appt_id}/reschedule")
async def patient_reschedule_appointment(appt_id: str, req: PatientRescheduleRequest, request: Request):
    """Cancel old appointment and book same doctor at new_slot."""
    user = _get_user_from_request(request)
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("patient_user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="Not your appointment.")
    if appt.get("status") not in (None, "upcoming", "confirmed"):
        raise HTTPException(status_code=400, detail="Only upcoming appointments can be rescheduled.")

    doctors = _load_doctors()
    doc = next((d for d in doctors if d["id"] == appt.get("doctor_id")), None)
    if doc is None:
        raise HTTPException(status_code=400, detail="Doctor not found.")
    if req.new_slot not in doc.get("available_slots", []):
        raise HTTPException(status_code=400, detail="Slot is no longer available.")

    # Mark old appointment cancelled and return its slot
    old_slot = appt.get("slot", "")
    appt["status"] = "cancelled"
    appt["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    appt["cancelled_by"] = "patient_reschedule"
    _save_appointments(appointments)
    if old_slot:
        slots_set = set(doc.get("available_slots", []))
        slots_set.add(old_slot)
        doc["available_slots"] = sorted(slots_set)

    # Remove new slot from doctor's available pool
    available = doc.get("available_slots", [])
    if req.new_slot in available:
        available.remove(req.new_slot)
    doc["available_slots"] = available
    _save_doctors(doctors)

    # Create new appointment
    new_appt_id = str(uuid.uuid4())
    new_appt = {
        "appointment_id": new_appt_id,
        "session_id": appt.get("session_id", ""),
        "patient_user_id": user["id"],
        "patient_name": user.get("name", ""),
        "doctor_id": doc["id"],
        "doctor_name": doc.get("name", ""),
        "specialty": doc.get("specialty", appt.get("specialty", "")),
        "slot": req.new_slot,
        "status": "upcoming",
        "note": req.note or appt.get("note", ""),
        "booked_at": datetime.now(timezone.utc).isoformat(),
        "rescheduled_from": appt_id,
        "is_followup": appt.get("is_followup", False),
        "parent_appointment_id": appt.get("parent_appointment_id", ""),
        "patient_files": appt.get("patient_files", []),
        "primary_diagnosis": appt.get("primary_diagnosis", ""),
        "urgency": appt.get("urgency", "routine"),
        "chief_complaint": appt.get("chief_complaint", ""),
    }
    appointments = _load_appointments()
    appointments[new_appt_id] = new_appt
    _save_appointments(appointments)
    return {"rescheduled": True, "new_appt_id": new_appt_id, "new_slot": req.new_slot}


# ---------------------------------------------------------------------------
# Feature: Post-visit rating (patient submits, doctor reads)
# ---------------------------------------------------------------------------

class RatingRequest(BaseModel):
    rating: int        # 1-5
    comment: str = ""

@app.post("/api/patient/appointments/{appt_id}/rating")
async def submit_rating(appt_id: str, req: RatingRequest, request: Request):
    """Patient submits a 1-5 star rating after a visit."""
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    if not (1 <= req.rating <= 5):
        raise HTTPException(status_code=400, detail="Rating must be 1-5.")
    appointments = _load_appointments()
    appt = appointments.get(appt_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.get("patient_user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not your appointment.")
    if appt.get("status") != "seen":
        raise HTTPException(status_code=400, detail="Can only rate completed visits.")
    appt["rating"] = {"stars": req.rating, "comment": req.comment, "submitted_at": datetime.now(timezone.utc).isoformat()}
    _save_appointments(appointments)
    return {"saved": True}


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
