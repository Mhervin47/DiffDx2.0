"""Phase C, sub-slice 1 of the appointments cutover: _compose_appointment_dict
(web/api.py), built and tested here in isolation — NOT wired into any route
yet (see TASK13_APPOINTMENTS_COMPOSER.md). These tests exercise the
relationally-sourced half of the composer (core fields, patient/doctor name
resolution, referral, second opinion, rating) against an isolated SQLite
engine, same pattern as test_appointment_repositories.py. The blob-sourced
half (test_orders/prescriptions/approved_plan/patient_files/the ~8
no-column fields) is verified separately via a live diff against real
dual-written data — nothing here asserts specific blob-store content, since
_load_appointments() reads the real dev DB, not this module's isolated
engine (see the plan's "live check" verification step)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from diffdx.db import models  # noqa: F401 — registers all models on Base
from diffdx.db.base import Base
from diffdx.db.models.user import Doctor, Patient, User
from diffdx.repositories.appointments import AppointmentRepository
from diffdx.repositories.clinical import ReferralRepository, SecondOpinionRepository


@pytest.fixture
def session_maker(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _make_patient(session_maker, name: str = "Test Patient") -> uuid.UUID:
    patient_id = uuid.uuid4()
    with session_maker() as session:
        session.add(User(id=patient_id, name=name, email=f"p.{patient_id}@test.com", password_hash="x", role="patient"))
        session.flush()
        session.add(Patient(user_id=patient_id))
        session.commit()
    return patient_id


def _make_doctor(session_maker, doctor_id: str = "dr_test", name: str = "Test Doctor", specialty: str = "General") -> uuid.UUID:
    user_id = uuid.uuid4()
    with session_maker() as session:
        session.add(User(id=user_id, name=name, email=f"d.{user_id}@test.com", password_hash="x", role="doctor"))
        session.flush()
        session.add(Doctor(user_id=user_id, doctor_id=f"{doctor_id}_{user_id.hex[:6]}", specialty=specialty))
        session.commit()
    return user_id


def test_compose_minimal_appointment_leaves_optional_fields_none(session_maker):
    """A bare booking (no referral/second-opinion/rating) should compose
    without raising, with those optional fields coming back None rather
    than a KeyError or an empty-dict placeholder."""
    from web.api import _compose_appointment_dict

    patient_id = _make_patient(session_maker, "Jane Patient")
    doctor_id = _make_doctor(session_maker, "dr_001", "Dr. Sarah Chen", "Cardiology")
    slot = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)

    with session_maker() as session:
        appt = AppointmentRepository(session).book(patient_id=patient_id, doctor_id=doctor_id, slot_datetime=slot)
        session.commit()

        composed = _compose_appointment_dict(session, appt)

    assert composed["appointment_id"] == str(appt.id)
    assert composed["patient_user_id"] == str(patient_id)
    assert composed["patient_name"] == "Jane Patient"
    assert composed["doctor_name"] == "Dr. Sarah Chen"
    assert composed["specialty"] == "Cardiology"
    assert composed["doctor_id"].startswith("dr_001_")
    assert composed["slot"] == slot.strftime("%Y-%m-%dT%H:%M")
    assert composed["status"] == "upcoming"
    assert composed["urgency"] == "routine"
    assert composed["is_followup"] is False
    assert composed["parent_appointment_id"] is None
    assert composed["rescheduled_from_id"] is None
    assert composed["referral"] is None
    assert composed["second_opinion"] is None
    assert composed["rating"] is None
    # List-shaped blob-sourced fields degrade gracefully to [] when no
    # matching blob record exists (a purely-relational test appointment) —
    # the composer explicitly guards these against KeyError. Scalar/dict
    # no-column fields (doctor_notes, etc.) are simply absent in that case,
    # same as how the blob itself behaves for a record that never set
    # them — every real read call site already accesses these via .get(),
    # never a bare key, so this matches existing behavior exactly.
    assert composed["test_orders"] == []
    assert composed["patient_files"] == []
    assert composed.get("doctor_notes") is None


def test_compose_appointment_with_referral_second_opinion_and_rating(session_maker):
    """Every relationally-sourced sub-entity round-trips through the
    composer correctly when present."""
    from web.api import _compose_appointment_dict

    patient_id = _make_patient(session_maker)
    from_doctor_id = _make_doctor(session_maker, "dr_from", "Dr. From", "Neurology")
    to_doctor_id = _make_doctor(session_maker, "dr_to", "Dr. To", "Radiology")
    slot = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)

    with session_maker() as session:
        appt = AppointmentRepository(session).book(patient_id=patient_id, doctor_id=from_doctor_id, slot_datetime=slot)
        ReferralRepository(session).upsert(
            appt.id, specialty="Cardiology", to_doctor="Dr. Chen",
            urgency="urgent", notes="please evaluate", internal_note="flagged for follow-up",
        )
        SecondOpinionRepository(session).create(
            appt.id, from_doctor_id, to_doctor_id, patient_summary="Chest pain, 3 days", note="second look needed",
        )
        submitted_at = datetime.now(timezone.utc).replace(microsecond=0)
        AppointmentRepository(session).set_rating(appt.id, stars=4, comment="Good visit", submitted_at=submitted_at)
        session.commit()

        composed = _compose_appointment_dict(session, AppointmentRepository(session).get_by_id(appt.id))

    assert composed["referral"] == {
        "specialty": "Cardiology", "to_doctor": "Dr. Chen", "urgency": "urgent",
        "notes": "please evaluate", "internal_note": "flagged for follow-up",
        "referred_at": composed["referral"]["referred_at"],  # server-set, just confirm the key exists
    }
    assert composed["second_opinion"]["to_doctor_name"] == "Dr. To"
    assert composed["second_opinion"]["status"] == "pending"
    assert composed["second_opinion"]["to_doctor_id"].startswith("dr_to_")
    assert composed["rating"] == {"stars": 4, "comment": "Good visit", "submitted_at": submitted_at.isoformat()}


def test_compose_appointment_reflects_cancellation_and_followup_links(session_maker):
    from web.api import _compose_appointment_dict

    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    slot = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)

    with session_maker() as session:
        parent = AppointmentRepository(session).book(patient_id=patient_id, doctor_id=doctor_id, slot_datetime=slot)
        cancelled_at = datetime.now(timezone.utc).replace(microsecond=0)
        AppointmentRepository(session).cancel(parent.id, cancelled_by="patient", cancelled_at=cancelled_at)
        followup = AppointmentRepository(session).book(
            patient_id=patient_id, doctor_id=doctor_id,
            slot_datetime=slot + timedelta(days=7),
            is_followup=True, parent_appointment_id=parent.id, rescheduled_from_id=parent.id,
        )
        session.commit()

        composed_parent = _compose_appointment_dict(session, AppointmentRepository(session).get_by_id(parent.id))
        composed_followup = _compose_appointment_dict(session, AppointmentRepository(session).get_by_id(followup.id))

    assert composed_parent["status"] == "cancelled"
    assert composed_parent["cancelled_by"] == "patient"
    assert composed_parent["cancelled_at"] == cancelled_at.isoformat()
    assert composed_followup["is_followup"] is True
    assert composed_followup["parent_appointment_id"] == str(parent.id)
    assert composed_followup["rescheduled_from_id"] == str(parent.id)
