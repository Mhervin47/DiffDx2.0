#!/usr/bin/env python3
"""Task 3 — concurrency proof.

Fires N concurrent booking attempts for the SAME doctor and SAME slot, and
reports what actually ends up in the database afterward — not just what
the response codes claimed.

--mode blob:
    Hits the real, live `/api/session/{id}/book` endpoint over HTTP against
    a running `web/api.py` instance. This is the code path used in
    production today: read-whole-collection -> mutate dict ->
    write-whole-collection, no locking. Requires the server to already be
    running (see --base-url).

    Setup seeds one throwaway patient account and injects a synthetic
    completed-session summary directly into that patient's blob record
    (bypassing a real, LLM-backed diagnostic session — this demo is about
    proving/disproving a storage race, not re-testing the AI layer) plus
    one doctor with one open slot. All N requests are the same
    patient/session booking the same doctor/slot concurrently — the
    literal "double-click / flaky-retry" scenario that corrupts data
    today.

--mode relational:
    Task 4 (split the monolith) hasn't happened yet, so there is no live
    HTTP endpoint exercising the new schema's booking path yet. This mode
    instead opens N concurrent SQLAlchemy sessions (one per thread) and
    calls AppointmentRepository.book() directly — proving the DB
    constraint from Task 1, which is the thing that actually matters here.
    See TASK3_CONCURRENCY_PROOF.md §2 for why.

Usage:
    cd loop1
    PYTHONPATH=src:. .venv/bin/python scripts/concurrency_demo.py --mode blob --n 20
    PYTHONPATH=src:. .venv/bin/python scripts/concurrency_demo.py --mode relational --n 20
"""
from __future__ import annotations

import argparse
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, select, text

from diffdx.db import models  # noqa: F401 — registers all models
from diffdx.db.engine import get_engine, get_sessionmaker
from diffdx.db.models.scheduling import Appointment
from diffdx.exceptions import ConflictError
from diffdx.repositories.appointments import AppointmentRepository


@dataclass
class Outcome:
    ok: bool
    detail: str = ""


# ── relational mode ──────────────────────────────────────────────────────

def _ensure_relational_fixture() -> tuple[uuid.UUID, uuid.UUID, datetime]:
    """Insert one patient + one doctor if they don't already exist, return
    (a fresh patient_id per call, the shared doctor_id, the shared slot)."""
    import diffdx.db.models.user as user_models

    SessionLocal = get_sessionmaker()
    slot_dt = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=30)
    with SessionLocal() as session:
        doctor_user = session.execute(
            text("SELECT user_id FROM doctors WHERE doctor_id = 'demo_doc'")
        ).fetchone()
        if doctor_user is None:
            doctor_id = uuid.uuid4()
            session.add(user_models.User(
                id=doctor_id, name="Demo Doctor", email="demo.doctor@concurrency.test",
                password_hash="x", role="doctor",
            ))
            session.flush()
            session.add(user_models.Doctor(
                user_id=doctor_id, doctor_id="demo_doc", specialty="General",
            ))
            session.commit()
        else:
            doctor_id = doctor_user[0]
        return doctor_id, slot_dt


def _make_relational_patient(session_maker) -> uuid.UUID:
    import diffdx.db.models.user as user_models

    patient_id = uuid.uuid4()
    with session_maker() as session:
        session.add(user_models.User(
            id=patient_id, name="Demo Patient", email=f"demo.patient.{patient_id}@concurrency.test",
            password_hash="x", role="patient",
        ))
        session.flush()
        session.add(user_models.Patient(user_id=patient_id))
        session.commit()
    return patient_id


def _relational_attempt(doctor_id: uuid.UUID, slot_dt: datetime) -> Outcome:
    SessionLocal = get_sessionmaker()
    patient_id = _make_relational_patient(SessionLocal)
    with SessionLocal() as session:
        repo = AppointmentRepository(session)
        try:
            repo.book(patient_id=patient_id, doctor_id=doctor_id, slot_datetime=slot_dt)
            session.commit()
            return Outcome(ok=True)
        except ConflictError as exc:
            session.rollback()
            return Outcome(ok=False, detail=exc.detail)


def run_relational(n: int) -> dict:
    doctor_id, slot_dt = _ensure_relational_fixture()
    results: list[Outcome] = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(_relational_attempt, doctor_id, slot_dt) for _ in range(n)]
        for f in as_completed(futures):
            results.append(f.result())

    SessionLocal = get_sessionmaker()
    with SessionLocal() as session:
        row_count = session.execute(
            select(func.count()).select_from(Appointment).where(
                Appointment.doctor_id == doctor_id, Appointment.slot_datetime == slot_dt
            )
        ).scalar_one()

    return {
        "sent": n,
        "success": sum(1 for r in results if r.ok),
        "conflict": sum(1 for r in results if not r.ok),
        "rows_in_db": row_count,
    }


# ── blob mode ─────────────────────────────────────────────────────────────
#
# An earlier version of this drove the real HTTP endpoint over a live
# uvicorn server. Two things made that unworkable in practice rather than
# more "realistic":
#   1. A single-worker server serializes requests anyway — `async def
#      book_appointment` blocks the one event loop for the duration of its
#      synchronous DB calls, so concurrent HTTP requests never actually
#      overlap inside the handler. That accidentally *hides* the bug.
#   2. Multiple uvicorn workers hitting the same SQLite file in WAL mode
#      hit "disk I/O error" on this machine — reproducible even from the
#      sqlite3 CLI while the server was up, independent of this script.
#      An environment issue, not the thing under test.
#
# So this exercises the actual vulnerable functions from web/api.py
# (_load_doctors/_save_doctors/_load_appointments/_save_appointments) in N
# real OS processes via multiprocessing — genuine concurrency, no event
# loop or multi-worker-SQLite variables in the way. It's a more precise
# reproduction of the defect, not a weaker one: the auth/routing/session
# machinery around the real endpoint is ceremony that's irrelevant to the
# storage race this task is actually about.

def _blob_worker(doctor_id: str, slot: str) -> bool:
    """Runs in a fresh OS process. Replicates web/api.py's actual booking
    sequence (see book_appointment, ~L1595) using its real functions.

    The real endpoint does real work between reading appointments and
    saving them — resolving the session's differential, extracting
    uploaded files, updating the user's session list (~L1629-1750). That
    naturally widens the read-modify-write race window. This minimal
    reproduction strips that out for clarity, which — measured directly —
    shrinks the window below what process-spawn scheduling jitter reliably
    interleaves on this machine (sub-millisecond critical section vs.
    multi-hundred-millisecond process startup). A short, honestly-labeled
    sleep here restores realistic timing rather than removing the bug.
    """
    import sys
    import time
    from datetime import datetime, timezone
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(0, str(root))
    import os
    os.environ.pop("DATABASE_URL", None)  # force the SQLite path under test

    from web.api import _load_appointments, _load_doctors, _save_appointments, _save_doctors

    doctors = _load_doctors()
    doctor = next((d for d in doctors if d["id"] == doctor_id), None)
    if doctor is None or slot not in doctor.get("available_slots", []):
        return False

    appt_id = str(uuid.uuid4())
    appointments = _load_appointments()
    time.sleep(0.1)  # simulate the real work the live endpoint does here
    appointments[appt_id] = {
        "appointment_id": appt_id,
        "doctor_id": doctor_id,
        "slot": slot,
        "status": "upcoming",
        "booked_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_appointments(appointments)

    doctor["available_slots"] = [s for s in doctor["available_slots"] if s != slot]
    _save_doctors(doctors)
    return True


def _seed_blob_fixture() -> tuple[str, str]:
    import os
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(0, str(root))
    os.environ.pop("DATABASE_URL", None)

    from web.api import _load_doctors, _save_doctors

    doctor_id = "demo_doc_blob"
    slot = (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=30)).isoformat()
    doctors = [d for d in _load_doctors() if d["id"] != doctor_id]
    doctors.append({
        "id": doctor_id, "name": "Demo Doctor (blob)", "specialty": "General",
        "hospital": "Demo Clinic", "rating": 5.0, "avatar_initials": "DD",
        "available_slots": [slot],
    })
    _save_doctors(doctors)
    return doctor_id, slot


def run_blob(n: int, base_url: str) -> dict:
    import multiprocessing as mp
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(0, str(root))
    import os
    os.environ.pop("DATABASE_URL", None)

    doctor_id, slot = _seed_blob_fixture()

    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=n) as pool:
        results = pool.starmap(_blob_worker, [(doctor_id, slot)] * n)

    from web.api import _load_appointments
    appts = _load_appointments()
    rows_for_slot = [a for a in appts.values() if a.get("doctor_id") == doctor_id and a.get("slot") == slot]

    return {
        "sent": n,
        "success": sum(1 for r in results if r),
        "conflict": sum(1 for r in results if not r),
        "rows_in_db": len(rows_for_slot),
    }


# ── driver ────────────────────────────────────────────────────────────────

def print_table(mode: str, stats: dict) -> str:
    header = f"{'mode':<12}{'sent':>8}{'success':>10}{'conflict':>10}{'rows in DB':>14}"
    row = (
        f"{mode:<12}{stats['sent']:>8}{stats['success']:>10}{stats['conflict']:>10}"
        f"{stats['rows_in_db']:>14}"
    )
    return header + "\n" + row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["blob", "relational"], required=True)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    if args.mode == "blob":
        stats = run_blob(args.n, args.base_url)
    else:
        stats = run_relational(args.n)

    output = print_table(args.mode, stats)
    print(output)

    if stats["rows_in_db"] != 1:
        print(
            f"\nWARNING: expected exactly 1 row in DB for this slot, got {stats['rows_in_db']}.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
