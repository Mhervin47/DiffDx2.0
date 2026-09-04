"""Phase A of the appointments cutover: repository layer for every
sub-entity table an Appointment fans out into (SuggestedTest, Prescription,
Referral, TreatmentPlanItem, SecondOpinion, Waitlist, BlockedDate), plus the
NotFoundError fix on AppointmentRepository.cancel/set_rating. Nothing routes
through this code yet (that's Phase B) — these are direct repository-level
tests against an isolated SQLite engine, same pattern as
test_concurrency.py's pg_sessionmaker fixture but local (no race conditions
to prove here, just CRUD correctness)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from diffdx.db import models  # noqa: F401 — registers all models on Base
from diffdx.db.base import Base
from diffdx.db.models.clinical import Referral
from diffdx.db.models.user import Doctor, Patient, User
from diffdx.exceptions import NotFoundError
from diffdx.repositories.appointments import AppointmentRepository
from diffdx.repositories.clinical import (
    PrescriptionRepository,
    ReferralRepository,
    SecondOpinionRepository,
    SuggestedTestRepository,
    TreatmentPlanItemRepository,
)
from diffdx.repositories.scheduling import BlockedDateRepository, WaitlistRepository


@pytest.fixture
def session_maker(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _make_patient(session_maker) -> uuid.UUID:
    patient_id = uuid.uuid4()
    with session_maker() as session:
        session.add(User(id=patient_id, name="Test Patient", email=f"p.{patient_id}@test.com", password_hash="x", role="patient"))
        session.flush()
        session.add(Patient(user_id=patient_id))
        session.commit()
    return patient_id


def _make_doctor(session_maker, doctor_id: str = "dr_test") -> uuid.UUID:
    user_id = uuid.uuid4()
    with session_maker() as session:
        session.add(User(id=user_id, name="Test Doctor", email=f"d.{user_id}@test.com", password_hash="x", role="doctor"))
        session.flush()
        session.add(Doctor(user_id=user_id, doctor_id=f"{doctor_id}_{user_id.hex[:6]}", specialty="General"))
        session.commit()
    return user_id


def _make_appointment(session_maker, patient_id: uuid.UUID, doctor_id: uuid.UUID, slot: datetime | None = None) -> uuid.UUID:
    with session_maker() as session:
        appt = AppointmentRepository(session).book(
            patient_id=patient_id, doctor_id=doctor_id,
            slot_datetime=slot or datetime.now(timezone.utc) + timedelta(days=1),
        )
        session.commit()
        return appt.id


# ---------------------------------------------------------------------------
# SuggestedTestRepository
# ---------------------------------------------------------------------------

def test_suggested_test_create_and_list(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        repo = SuggestedTestRepository(session)
        repo.create(appt_id, test="CBC", category="blood", priority="urgent", notes="fasting required")
        session.commit()

    with session_maker() as session:
        tests = SuggestedTestRepository(session).list_for_appointment(appt_id)
        assert len(tests) == 1
        assert tests[0].test == "CBC"
        assert tests[0].category == "blood"
        assert tests[0].priority == "urgent"
        assert tests[0].notes == "fasting required"
        assert tests[0].result is None


def test_suggested_test_replace_for_appointment_swaps_whole_list(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        repo = SuggestedTestRepository(session)
        repo.replace_for_appointment(appt_id, [
            {"test": "CBC", "category": "blood"},
            {"test": "Chest X-ray", "category": "imaging"},
        ])
        session.commit()

    with session_maker() as session:
        tests = SuggestedTestRepository(session).list_for_appointment(appt_id)
        assert {t.test for t in tests} == {"CBC", "Chest X-ray"}

    # Replacing again drops the old list entirely, not appends.
    with session_maker() as session:
        SuggestedTestRepository(session).replace_for_appointment(appt_id, [{"test": "ECG", "category": "other"}])
        session.commit()

    with session_maker() as session:
        tests = SuggestedTestRepository(session).list_for_appointment(appt_id)
        assert [t.test for t in tests] == ["ECG"]


def test_suggested_test_record_result(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        test_dto = SuggestedTestRepository(session).create(appt_id, test="CBC", category="blood")
        session.commit()
        test_id = test_dto.id

    with session_maker() as session:
        updated = SuggestedTestRepository(session).record_result(
            test_id, result="Normal range", result_status="normal", result_recorded_at=datetime.now(timezone.utc),
        )
        session.commit()
        assert updated.result == "Normal range"
        assert updated.result_status == "normal"


def test_suggested_test_record_result_missing_raises_not_found(session_maker):
    with session_maker() as session:
        with pytest.raises(NotFoundError):
            SuggestedTestRepository(session).record_result(uuid.uuid4(), result="x", result_status="normal")


# ---------------------------------------------------------------------------
# PrescriptionRepository
# ---------------------------------------------------------------------------

def test_prescription_create_and_replace(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        repo = PrescriptionRepository(session)
        repo.create(appt_id, drug="Amoxicillin", dose="500mg", frequency="TID", duration="7 days")
        session.commit()

    with session_maker() as session:
        rx = PrescriptionRepository(session).list_for_appointment(appt_id)
        assert len(rx) == 1
        assert rx[0].drug == "Amoxicillin"
        assert rx[0].route == "oral"  # default

    with session_maker() as session:
        PrescriptionRepository(session).replace_for_appointment(appt_id, [
            {"drug": "Ibuprofen", "dose": "200mg", "frequency": "BID", "duration": "3 days", "route": "oral"},
        ])
        session.commit()

    with session_maker() as session:
        rx = PrescriptionRepository(session).list_for_appointment(appt_id)
        assert [r.drug for r in rx] == ["Ibuprofen"]


# ---------------------------------------------------------------------------
# ReferralRepository
# ---------------------------------------------------------------------------

def test_referral_upsert_creates_then_updates_without_duplicating(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        ReferralRepository(session).upsert(appt_id, specialty="Cardiology", to_doctor="Dr. Chen", notes="urgent eval")
        session.commit()

    with session_maker() as session:
        ref = ReferralRepository(session).get_for_appointment(appt_id)
        assert ref.specialty == "Cardiology"
        assert ref.to_doctor == "Dr. Chen"

    # Second upsert overwrites in place — no duplicate row (unique appointment_id).
    with session_maker() as session:
        ReferralRepository(session).upsert(appt_id, specialty="Neurology", urgency="urgent")
        session.commit()

    with session_maker() as session:
        from sqlalchemy import func, select as sa_select
        count = session.execute(
            sa_select(func.count()).select_from(Referral).where(Referral.appointment_id == appt_id)
        ).scalar_one()
        assert count == 1
        ref = ReferralRepository(session).get_for_appointment(appt_id)
        assert ref.specialty == "Neurology"
        assert ref.urgency == "urgent"
        assert ref.to_doctor is None  # overwritten, not merged


# ---------------------------------------------------------------------------
# TreatmentPlanItemRepository
# ---------------------------------------------------------------------------

def test_treatment_plan_item_create_and_replace(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        TreatmentPlanItemRepository(session).replace_for_appointment(appt_id, [
            {"text": "Rest and hydration", "approved": True, "source": "ai"},
            {"text": "Follow-up in 2 weeks", "approved": False, "source": "doctor"},
        ])
        session.commit()

    with session_maker() as session:
        items = TreatmentPlanItemRepository(session).list_for_appointment(appt_id)
        assert len(items) == 2
        by_text = {i.text: i for i in items}
        assert by_text["Rest and hydration"].approved is True
        assert by_text["Follow-up in 2 weeks"].approved is False
        assert by_text["Follow-up in 2 weeks"].source == "doctor"


# ---------------------------------------------------------------------------
# SecondOpinionRepository
# ---------------------------------------------------------------------------

def test_second_opinion_lifecycle(session_maker):
    patient_id = _make_patient(session_maker)
    from_doctor = _make_doctor(session_maker, "dr_from")
    to_doctor = _make_doctor(session_maker, "dr_to")
    appt_id = _make_appointment(session_maker, patient_id, from_doctor)

    with session_maker() as session:
        opinion = SecondOpinionRepository(session).create(
            appt_id, from_doctor, to_doctor, patient_summary="45yo with chest pain",
        )
        session.commit()
        opinion_id = opinion.id
        assert opinion.status == "pending"

    with session_maker() as session:
        inbox = SecondOpinionRepository(session).list_inbox_for_doctor(to_doctor)
        assert len(inbox) == 1
        assert inbox[0].id == opinion_id

    with session_maker() as session:
        responded = SecondOpinionRepository(session).respond(
            opinion_id, response="Recommend stress test", responded_at=datetime.now(timezone.utc),
        )
        session.commit()
        assert responded.status == "responded"
        assert responded.response == "Recommend stress test"


def test_second_opinion_respond_missing_raises_not_found(session_maker):
    with session_maker() as session:
        with pytest.raises(NotFoundError):
            SecondOpinionRepository(session).respond(uuid.uuid4(), response="x", responded_at=datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# WaitlistRepository
# ---------------------------------------------------------------------------

def test_waitlist_join_list_leave(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)

    with session_maker() as session:
        entry = WaitlistRepository(session).join(patient_id, doctor_id, note="prefer mornings")
        session.commit()
        entry_id = entry.id
        assert entry.status == "waiting"

    with session_maker() as session:
        assert len(WaitlistRepository(session).list_for_patient(patient_id)) == 1
        assert len(WaitlistRepository(session).list_for_doctor(doctor_id)) == 1
        assert len(WaitlistRepository(session).list_for_doctor(doctor_id, status="waiting")) == 1

    with session_maker() as session:
        assert WaitlistRepository(session).leave(entry_id) is True
        session.commit()

    with session_maker() as session:
        assert len(WaitlistRepository(session).list_for_doctor(doctor_id, status="waiting")) == 0
        assert len(WaitlistRepository(session).list_for_doctor(doctor_id, status="cancelled")) == 1

    with session_maker() as session:
        assert WaitlistRepository(session).leave(uuid.uuid4()) is False


def test_waitlist_notify_next_picks_earliest_waiting_and_skips_notified(session_maker):
    doctor_id = _make_doctor(session_maker)
    patient1 = _make_patient(session_maker)
    patient2 = _make_patient(session_maker)

    with session_maker() as session:
        repo = WaitlistRepository(session)
        first = repo.join(patient1, doctor_id)
        session.commit()
    with session_maker() as session:
        repo = WaitlistRepository(session)
        second = repo.join(patient2, doctor_id)
        session.commit()

    with session_maker() as session:
        notified = WaitlistRepository(session).notify_next(doctor_id)
        session.commit()
        assert notified.id == first.id
        assert notified.status == "notified"

    # Second call skips the now-notified first entry and picks the second.
    with session_maker() as session:
        notified_again = WaitlistRepository(session).notify_next(doctor_id)
        session.commit()
        assert notified_again.id == second.id

    # No more waiting entries left.
    with session_maker() as session:
        assert WaitlistRepository(session).notify_next(doctor_id) is None


# ---------------------------------------------------------------------------
# BlockedDateRepository
# ---------------------------------------------------------------------------

def test_blocked_date_block_list_unblock(session_maker):
    doctor_id = _make_doctor(session_maker)

    with session_maker() as session:
        repo = BlockedDateRepository(session)
        assert repo.is_blocked(doctor_id, "2026-12-25") is False
        repo.block(doctor_id, "2026-12-25", reason="Holiday")
        session.commit()

    with session_maker() as session:
        repo = BlockedDateRepository(session)
        assert repo.is_blocked(doctor_id, "2026-12-25") is True
        dates = repo.list_for_doctor(doctor_id)
        assert len(dates) == 1
        assert dates[0].reason == "Holiday"

    with session_maker() as session:
        repo = BlockedDateRepository(session)
        assert repo.unblock(doctor_id, "2026-12-25") is True
        session.commit()

    with session_maker() as session:
        repo = BlockedDateRepository(session)
        assert repo.is_blocked(doctor_id, "2026-12-25") is False
        assert repo.unblock(doctor_id, "2026-12-25") is False  # already gone


# ---------------------------------------------------------------------------
# AppointmentRepository.cancel/set_rating — NotFoundError, not bare ValueError
# ---------------------------------------------------------------------------

def test_appointment_cancel_missing_raises_not_found(session_maker):
    with session_maker() as session:
        with pytest.raises(NotFoundError):
            AppointmentRepository(session).cancel(uuid.uuid4(), cancelled_by="patient", cancelled_at=datetime.now(timezone.utc))


def test_appointment_set_rating_missing_raises_not_found(session_maker):
    with session_maker() as session:
        with pytest.raises(NotFoundError):
            AppointmentRepository(session).set_rating(uuid.uuid4(), stars=5, comment="great", submitted_at=datetime.now(timezone.utc))


def test_appointment_cancel_success(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        cancelled = AppointmentRepository(session).cancel(appt_id, cancelled_by="patient", cancelled_at=datetime.now(timezone.utc))
        session.commit()
        assert cancelled.status == "cancelled"
        assert cancelled.cancelled_by == "patient"
