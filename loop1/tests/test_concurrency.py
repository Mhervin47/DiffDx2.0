from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from diffdx.db import models  # noqa: F401 — registers all models
from diffdx.db.base import Base
from diffdx.exceptions import ConflictError
from diffdx.repositories.appointments import AppointmentRepository

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"),
    reason="Task 3's concurrency proof needs real overlapping Postgres transactions — "
    "SQLite's single-writer serialization can mask the exact race this proves doesn't "
    "happen. Skipped unless DATABASE_URL points at a real Postgres instance.",
)


@pytest.fixture
def pg_sessionmaker():
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _make_patient(session_maker) -> uuid.UUID:
    from diffdx.db.models.user import Patient, User

    patient_id = uuid.uuid4()
    with session_maker() as session:
        session.add(User(
            id=patient_id, name="Test Patient", email=f"test.{patient_id}@concurrency.test",
            password_hash="x", role="patient",
        ))
        session.flush()
        session.add(Patient(user_id=patient_id))
        session.commit()
    return patient_id


def _make_doctor(session_maker) -> uuid.UUID:
    from diffdx.db.models.user import Doctor, User

    doctor_id = uuid.uuid4()
    with session_maker() as session:
        session.add(User(
            id=doctor_id, name="Test Doctor", email=f"test.doc.{doctor_id}@concurrency.test",
            password_hash="x", role="doctor",
        ))
        session.flush()
        session.add(Doctor(user_id=doctor_id, doctor_id=f"doc_{doctor_id.hex[:8]}", specialty="General"))
        session.commit()
    return doctor_id


def _attempt(session_maker, doctor_id: uuid.UUID, slot_dt: datetime) -> bool:
    patient_id = _make_patient(session_maker)
    with session_maker() as session:
        repo = AppointmentRepository(session)
        try:
            repo.book(patient_id=patient_id, doctor_id=doctor_id, slot_datetime=slot_dt)
            session.commit()
            return True
        except ConflictError:
            session.rollback()
            return False


def test_concurrent_booking_exactly_one_survives(pg_sessionmaker):
    """N=10 concurrent booking attempts, same doctor + slot, against real
    Postgres. Asserts exactly one success and clean ConflictErrors for the
    rest — no unhandled exceptions, no double-booking. This is the
    guarantee Task 1's UNIQUE (doctor_id, slot_datetime) partial index
    exists to provide; see docs/evidence/concurrency.txt for the N=20 demo
    output this same assertion is based on."""
    n = 10
    doctor_id = _make_doctor(pg_sessionmaker)
    slot_dt = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=30)

    results: list[bool] = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(_attempt, pg_sessionmaker, doctor_id, slot_dt) for _ in range(n)]
        for f in as_completed(futures):
            results.append(f.result())  # .result() re-raises anything unhandled

    successes = sum(results)
    conflicts = n - successes

    assert successes == 1, f"expected exactly 1 success, got {successes}"
    assert conflicts == n - 1, f"expected {n - 1} clean conflicts, got {conflicts}"

    from diffdx.db.models.scheduling import Appointment
    from sqlalchemy import func, select

    with pg_sessionmaker() as session:
        row_count = session.execute(
            select(func.count()).select_from(Appointment).where(
                Appointment.doctor_id == doctor_id, Appointment.slot_datetime == slot_dt
            )
        ).scalar_one()
    assert row_count == 1, f"expected exactly 1 row in the database, got {row_count}"
