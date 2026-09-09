"""App factory — FastAPI() instantiation, middleware, exception handlers,
router registration, and the lifespan context manager. week1.md Task 4's
target layout (`main.py — app factory, middleware, exception handlers,
lifespan`); previously all of this lived in web/api.py alongside ~90
routes and a pile of shared helpers. web/api.py is now a thin shim
(`from diffdx.main import app`) kept only because every deploy config
(Procfile, render.yaml, Dockerfile) already targets `web.api:app`, and
because tests/scripts still do `from web.api import <helper>`.

This is the first diffdx.* code that runs in the whole app — the .env
load + CRITIC_MODEL default must happen here, before anything else
(routers, diffdx.config, etc.) gets imported.
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure src/ is on the path before importing loop1 modules — belt and
# suspenders alongside PYTHONPATH=src, which every real entry point
# (Procfile, render.yaml, Dockerfile, pytest) already sets independently.
_repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo_root / "src"))

# Load .env
try:
    import dotenv
    dotenv.load_dotenv(_repo_root / ".env")
except ImportError:
    pass

os.environ.setdefault("CRITIC_MODEL", "openrouter/meta-llama/llama-3.3-70b-instruct")

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from diffdx.api_exceptions import register_exception_handlers
from diffdx.audit import log_audit_event
from diffdx.config import settings as _settings
from diffdx.db.engine import get_sessionmaker
from diffdx.legacy_store import (
    _ensure_relational_appointment,
    _get_user_from_request,
    _load_appointments,
    _load_users,
    _save_appointments,
    _static_dir,
)
from diffdx.rate_limit import limiter as _limiter
from diffdx.repositories.appointments import AppointmentRepository

from diffdx.routers.appointments import router as appointments_router
from diffdx.routers.appointments2 import router as appointments2_router
from diffdx.routers.appointments3 import router as appointments3_router
from diffdx.routers.appointments4 import router as appointments4_router
from diffdx.routers.appointments5 import router as appointments5_router
from diffdx.routers.appointments6 import router as appointments6_router
from diffdx.routers.auth import router as auth_router
from diffdx.routers.doctors import router as doctors_router
from diffdx.routers.health import router as health_router
from diffdx.routers.messaging import router as messaging_router
from diffdx.routers.pages import router as pages_router
from diffdx.routers.session_test_files import router as session_test_files_router
from diffdx.routers.session_booking import router as session_booking_router
from diffdx.routers.sessions import router as sessions_router
from diffdx.routers.tts import router as tts_router

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger(__name__)


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
                    try:
                        with get_sessionmaker()() as db:
                            appt_uuid = _ensure_relational_appointment(db, appt)
                            if appt_uuid is not None:
                                AppointmentRepository(db).mark_reminder_sent(appt_uuid)
                                db.commit()
                    except Exception:
                        _log.warning("Dual-write of reminder_sent failed for appt %s", appt_id, exc_info=True)
            if changed:
                _save_appointments(appointments)
        except Exception as exc:
            _log.warning("Reminder loop error: %s", exc)
        threading.Event().wait(3600)  # run once per hour


def _start_reminder_scheduler():
    t = threading.Thread(target=_reminder_loop, daemon=True, name="reminder-scheduler")
    t.start()
    _log.info("Appointment reminder scheduler started")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Auto-apply pending migrations to keep SQLite / Postgres in sync with models
    try:
        from alembic.config import Config
        from alembic import command
        alembic_cfg = Config(str(_repo_root / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(_repo_root / "alembic"))
        command.upgrade(alembic_cfg, "head")
    except Exception as e:
        _log.warning("Alembic auto-upgrade skipped or failed: %s", e)

    # Doctor seeding moved to scripts/seed_doctors.py (TASK4_SPLIT_ROUTERS.md
    # §2: seeding on every boot is a race in a multi-replica deployment) —
    # run it once against a fresh database instead; wired into
    # docker-entrypoint.sh and render.yaml's buildCommand.
    _settings.log_disabled_integrations()
    _start_reminder_scheduler()
    yield


app = FastAPI(title="DiffDx API", version="0.10.0", lifespan=_lifespan)

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
app.include_router(health_router)
app.include_router(messaging_router)
app.include_router(pages_router)
app.include_router(session_test_files_router)
app.include_router(sessions_router)
app.include_router(session_booking_router)
app.include_router(tts_router)

# ---------------------------------------------------------------------------
# Static files — MUST come last (catch-all)
# ---------------------------------------------------------------------------

if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
