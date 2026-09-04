"""Shared blob-store/auth/session helpers used by every router in
src/diffdx/routers/.

Task 4 split every route out of the original web/api.py monolith into
domain routers, but each router still reached back into web/api.py for
these helpers via a lazy `from web.api import X` *inside* each route
function body — necessary at the time because web/api.py imports and
registers the routers, so a top-level `from web.api import X` in a router
would be circular. TASK4_SPLIT_ROUTERS.md §2 flagged this as scaffolding,
not the end state.

This module holds every one of those shared names with zero dependency on
web.api or the FastAPI `app` object, so routers can import them normally
at module load time instead. web/api.py imports from here too (and, by
`from X import Y` binding `Y` in its own namespace, still supports
`from web.api import Y` for existing callers — tests and scripts that
import helpers directly from web.api keep working unchanged).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request

from diffdx.db.engine import get_sessionmaker
from diffdx.exceptions import ConflictError
from diffdx.repositories.appointments import AppointmentRepository
from diffdx.repositories.clinical import ReferralRepository, SecondOpinionRepository
from diffdx.repositories.users import UserRepository
from diffdx.session_store import get_session

_log = logging.getLogger(__name__)

# src/diffdx/legacy_store.py -> parents[0]=diffdx, [1]=src, [2]=repo root.
# Recomputed here rather than copied from web/api.py's own
# `_repo_root = Path(__file__).parent.parent`, which is web/api.py-relative
# and would resolve wrong at this file's location.
_repo_root = Path(__file__).resolve().parents[2]

_CASES_DIR = _repo_root / "test_cases"
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
_static_dir = _repo_root / "web" / "static"

# ---------------------------------------------------------------------------
# PostgreSQL data store — single table, one collection per row
# Reads DATABASE_URL from env (Railway sets this automatically).
# Falls back to SQLite for local dev if DATABASE_URL is not set.
# ---------------------------------------------------------------------------

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


def _load_waitlist() -> list:
    return _db_load("waitlist", [])


def _save_waitlist(entries: list) -> None:
    _db_save("waitlist", entries)


def _load_blocked_dates() -> dict:
    return _db_load("blocked_dates", {})


def _save_blocked_dates(data: dict) -> None:
    _db_save("blocked_dates", data)


def _dt_iso(dt) -> str | None:
    """isoformat(), treating a naive datetime as UTC first. SQLite (local
    dev) doesn't round-trip tzinfo on DateTime(timezone=True) columns the
    way Postgres (production) does — without this, the composer's output
    shape would differ by backend for every timestamp field."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _compose_appointment_dict(db, appt_dto) -> dict:
    """Phase C of the appointments cutover (read composer, see
    TASK13_APPOINTMENTS_COMPOSER.md): builds the legacy blob-shaped
    appointment dict from relational rows where that's safe, falling back
    to the appointment's own blob record for everything else. NOT wired
    into any route yet — this function exists and is tested in isolation
    only; flipping actual reads over is separate future work.

    Deliberately blob-sourced, not relational, even though this function
    is called "the composer":
    - test_orders/test_results_data/prescriptions/approved_plan: Task
      9/10's replace_for_appointment deletes+recreates these rows on
      every write, so their relational ids are NOT stable across saves
      and don't match what the blob (still the actual write target) has
      — serving a relational id here that a later write can't find would
      silently break "edit this item" flows. The blob remains the only
      stable identity source for these three lists.
    - patient_files: file-storage dual-write doesn't exist (Task 9/11
      deliberately excluded it) — there's no relational data to compose.
    - Every field with no relational column at all (reschedule_proposal,
      refill_request, intake, doctor_summary, doctor_notes, patient_tags,
      prescription_history, reminder_sent, suggested_test_uploads).
    """
    appt_id_str = str(appt_dto.id)
    blob_appt = _load_appointments().get(appt_id_str, {})

    patient_dto = UserRepository(db).get_by_id(appt_dto.patient_id)
    doctor_dto = UserRepository(db).get_by_id(appt_dto.doctor_id)

    referral_dto = ReferralRepository(db).get_for_appointment(appt_dto.id)
    referral = None
    if referral_dto is not None:
        # Merge onto the blob's own nested referral dict too — it carries
        # at least one field with no relational column at all
        # (referring_doctor, the doctor's display name at referral time),
        # same "never silently drop a blob-only field" reasoning as the
        # top-level composed dict.
        referral = dict(blob_appt.get("referral") or {})
        referral.update({
            "specialty": referral_dto.specialty,
            "to_doctor": referral_dto.to_doctor,
            "urgency": referral_dto.urgency,
            "notes": referral_dto.notes,
            "internal_note": referral_dto.internal_note,
            "referred_at": _dt_iso(referral_dto.referred_at),
        })

    second_opinion_dto = SecondOpinionRepository(db).get_for_appointment(appt_dto.id)
    second_opinion = None
    if second_opinion_dto is not None:
        to_doctor_dto = UserRepository(db).get_by_id(second_opinion_dto.to_doctor_id)
        second_opinion = dict(blob_appt.get("second_opinion") or {})
        second_opinion.update({
            "to_doctor_id": to_doctor_dto.doctor_id if to_doctor_dto else None,
            "to_doctor_name": to_doctor_dto.name if to_doctor_dto else None,
            "requested_at": _dt_iso(second_opinion_dto.requested_at),
            "status": second_opinion_dto.status,
            "opinion_id": str(second_opinion_dto.id),
        })

    rating = None
    if appt_dto.rating_stars is not None:
        rating = {
            "stars": appt_dto.rating_stars,
            "comment": appt_dto.rating_comment,
            "submitted_at": _dt_iso(appt_dto.rating_submitted_at),
        }

    # Start from the full blob record — this is what guarantees nothing is
    # ever silently dropped: bookkeeping/audit fields with no relational
    # column (is_direct_booking, patient_note, dependent_id,
    # booked_by_user_id, the *_updated_at timestamps, referring_doctor
    # nested in the blob's own "referral", etc.) ride along unchanged,
    # present or future, without needing to be individually enumerated
    # here. Only the fields this composer can source more reliably from
    # relational data are overridden below.
    composed = dict(blob_appt)
    # Guard against KeyError on the list-shaped fields for an appointment
    # that only exists relationally (no blob record at all yet, or one
    # that never happened to set these) — every existing read call site
    # already treats an absent list as empty via `.get(..., [])`, so this
    # just makes that the guaranteed shape instead of leaving the key out.
    for _list_field in ("test_orders", "test_results_data", "prescriptions",
                         "prescription_history", "approved_plan", "patient_files"):
        composed.setdefault(_list_field, [])
    composed.update({
        "appointment_id": appt_id_str,
        "session_id": appt_dto.session_id,
        "patient_user_id": str(appt_dto.patient_id),
        "patient_name": patient_dto.name if patient_dto else blob_appt.get("patient_name"),
        "doctor_id": doctor_dto.doctor_id if doctor_dto else blob_appt.get("doctor_id"),
        "doctor_name": doctor_dto.name if doctor_dto else blob_appt.get("doctor_name"),
        "specialty": doctor_dto.specialty if doctor_dto else blob_appt.get("specialty"),
        "slot": appt_dto.slot_datetime.strftime("%Y-%m-%dT%H:%M"),
        "status": appt_dto.status,
        "urgency": appt_dto.urgency,
        "note": appt_dto.note,
        "booked_at": _dt_iso(appt_dto.booked_at),
        "cancelled_at": _dt_iso(appt_dto.cancelled_at),
        "cancelled_by": appt_dto.cancelled_by,
        "is_followup": appt_dto.is_followup,
        "parent_appointment_id": str(appt_dto.parent_appointment_id) if appt_dto.parent_appointment_id else None,
        "rescheduled_from_id": str(appt_dto.rescheduled_from_id) if appt_dto.rescheduled_from_id else None,
        "chief_complaint": appt_dto.chief_complaint,
        "primary_diagnosis": appt_dto.primary_diagnosis,
        "age": appt_dto.patient_age,
        "sex": appt_dto.patient_sex,
        "bmi": appt_dto.patient_bmi,
        "rating": rating,
        "referral": referral,
        "second_opinion": second_opinion,
    })
    # test_orders/test_results_data/prescriptions/prescription_history/
    # approved_plan/patient_files are deliberately NOT overridden — see the
    # docstring above — they stay whatever the blob already had via the
    # dict(blob_appt) base, not touched here.
    return composed


def _load_session_uploads() -> dict:
    return _db_load("session_uploads", {})


def _save_session_uploads(data: dict) -> None:
    _db_save("session_uploads", data)


# Loaded from disk so uploads survive server restarts between upload and booking
_session_test_uploads: dict = _load_session_uploads()  # session_id → { test_id → record }


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
    session = get_session(session_id)
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


# Still used directly by routes that haven't moved to their own router yet —
# routers/doctors.py's require_role("doctor") dependency is the verified-
# equivalent replacement used by already-split routers.
def _require_doctor(request: Request):
    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    if user.get("role") != "doctor":
        raise HTTPException(status_code=403, detail="Doctor account required.")
    return user


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
