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
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from diffdx.db import models  # noqa: F401 — registers all models on Base
from diffdx.db.base import Base
from diffdx.db.models.clinical import Referral
from diffdx.db.models.user import Doctor, Patient, User
from diffdx.exceptions import ConflictError, NotFoundError
from diffdx.repositories.appointments import AppointmentRepository
from diffdx.repositories.clinical import (
    PrescriptionRepository,
    ReferralRepository,
    SecondOpinionRepository,
    SuggestedTestRepository,
    TreatmentPlanItemRepository,
)
from diffdx.repositories.files import FileRepository
from diffdx.repositories.scheduling import BlockedDateRepository, WaitlistRepository


@pytest.fixture
def session_maker(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})

    # SQLite doesn't enforce foreign-key constraints (incl. ON DELETE
    # CASCADE) unless this is set per-connection, unlike Postgres — same
    # fix applied to the app's real engine in diffdx/db/engine.py. This
    # fixture builds its own engine directly, so it needs its own copy.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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


def test_suggested_test_replace_for_appointment_handles_non_uuid_blob_ids(session_maker):
    """Blob test_orders ids are frequently short frontend-generated strings
    like "t1", not UUIDs — must not raise, must generate a fresh id."""
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        created = SuggestedTestRepository(session).replace_for_appointment(appt_id, [
            {"id": "t1", "test": "CBC", "category": "blood"},
        ])
        session.commit()
        assert created[0].test == "CBC"
        assert isinstance(created[0].id, uuid.UUID)  # a fresh UUID, not "t1"


def test_suggested_test_replace_for_appointment_with_merged_results(session_maker):
    """appointments2.py::update_test_results merges test_orders +
    test_results_data (joined by blob-native id) before calling
    replace_for_appointment — confirm result fields round-trip when present,
    and default to None when absent (doesn't change update_test_orders's
    existing orders-only behavior)."""
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)
    recorded_at = datetime.now(timezone.utc).isoformat()

    with session_maker() as session:
        created = SuggestedTestRepository(session).replace_for_appointment(appt_id, [
            {"id": "t1", "test": "CBC", "category": "blood", "result": "Normal", "result_status": "normal", "result_recorded_at": recorded_at},
            {"id": "t2", "test": "Chest X-ray", "category": "imaging"},  # no result yet
        ])
        session.commit()
        by_test = {t.test: t for t in created}
        assert by_test["CBC"].result == "Normal"
        assert by_test["CBC"].result_status == "normal"
        assert by_test["CBC"].result_recorded_at is not None
        assert by_test["Chest X-ray"].result is None
        assert by_test["Chest X-ray"].result_status is None


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

def test_waitlist_join_with_explicit_id(session_maker):
    """appointments5.py::join_waitlist reuses its own blob-generated entry
    id for the relational row too, so leave_waitlist can look it up by the
    same id later — confirm join() honors an explicit id."""
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    explicit_id = uuid.uuid4()

    with session_maker() as session:
        entry = WaitlistRepository(session).join(patient_id, doctor_id, id=explicit_id)
        session.commit()
        assert entry.id == explicit_id

    with session_maker() as session:
        assert WaitlistRepository(session).leave(explicit_id) is True


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


# ---------------------------------------------------------------------------
# AppointmentRepository.update_status / reschedule (Phase B, sub-slice 1)
# ---------------------------------------------------------------------------

def test_appointment_update_status_success(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        updated = AppointmentRepository(session).update_status(appt_id, "seen")
        session.commit()
        assert updated.status == "seen"


def test_appointment_update_status_missing_raises_not_found(session_maker):
    with session_maker() as session:
        with pytest.raises(NotFoundError):
            AppointmentRepository(session).update_status(uuid.uuid4(), "seen")


def test_appointment_reschedule_success(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    original_slot = datetime.now(timezone.utc) + timedelta(days=1)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id, slot=original_slot)
    new_slot = original_slot + timedelta(hours=1)

    with session_maker() as session:
        rescheduled = AppointmentRepository(session).reschedule(appt_id, new_slot)
        session.commit()
        assert rescheduled.slot_datetime == new_slot


def test_appointment_book_with_rescheduled_from_id(session_maker):
    """appointments4.py::patient_reschedule_appointment cancels the old
    appointment and books a genuinely new one linked via
    rescheduled_from_id — confirm book() accepts and persists it."""
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    old_slot = datetime.now(timezone.utc) + timedelta(days=1)
    old_appt_id = _make_appointment(session_maker, patient_id, doctor_id, slot=old_slot)

    with session_maker() as session:
        AppointmentRepository(session).cancel(old_appt_id, cancelled_by="patient_reschedule", cancelled_at=datetime.now(timezone.utc))
        new_appt = AppointmentRepository(session).book(
            patient_id=patient_id, doctor_id=doctor_id,
            slot_datetime=old_slot + timedelta(hours=1),
            rescheduled_from_id=old_appt_id,
        )
        session.commit()
        assert new_appt.rescheduled_from_id == old_appt_id


def test_appointment_reschedule_missing_raises_not_found(session_maker):
    with session_maker() as session:
        with pytest.raises(NotFoundError):
            AppointmentRepository(session).reschedule(uuid.uuid4(), datetime.now(timezone.utc))


def test_appointment_reschedule_into_taken_slot_raises_conflict(session_maker):
    patient1 = _make_patient(session_maker)
    patient2 = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    taken_slot = datetime.now(timezone.utc) + timedelta(days=2)
    _make_appointment(session_maker, patient1, doctor_id, slot=taken_slot)
    movable_appt_id = _make_appointment(session_maker, patient2, doctor_id, slot=taken_slot + timedelta(hours=1))

    with session_maker() as session:
        with pytest.raises(ConflictError):
            AppointmentRepository(session).reschedule(movable_appt_id, taken_slot)


# ---------------------------------------------------------------------------
# _ensure_relational_appointment (web/api.py) — self-heals a missing
# relational row from a blob-shaped appointment dict.
# ---------------------------------------------------------------------------

def test_ensure_relational_appointment_returns_existing_id_without_recreating(session_maker):
    from web.api import _ensure_relational_appointment

    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    blob_appt = {"appointment_id": str(appt_id), "patient_user_id": str(patient_id), "doctor_id": "irrelevant", "slot": "bad-data-should-not-be-parsed"}
    with session_maker() as session:
        result = _ensure_relational_appointment(session, blob_appt)
        assert result == appt_id


def test_ensure_relational_appointment_self_heals_missing_row(session_maker):
    from web.api import _ensure_relational_appointment

    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    # doctor_id in the repository lookup path is the SHORT legacy id, not the
    # relational UUID — fetch the real Doctor row's short id to build a
    # realistic blob dict.
    with session_maker() as session:
        doc = session.get(Doctor, doctor_id)
        short_doctor_id = doc.doctor_id

    new_appt_id = uuid.uuid4()
    blob_appt = {
        "appointment_id": str(new_appt_id),
        "patient_user_id": str(patient_id),
        "doctor_id": short_doctor_id,
        "slot": "2099-06-15T10:00",
        "status": "upcoming",
        "urgency": "routine",
    }
    with session_maker() as session:
        result = _ensure_relational_appointment(session, blob_appt)
        session.commit()
        assert result == new_appt_id

    with session_maker() as session:
        dto = AppointmentRepository(session).get_by_id(new_appt_id)
        assert dto is not None
        assert dto.patient_id == patient_id
        assert dto.doctor_id == doctor_id
        assert dto.status == "upcoming"


def test_ensure_relational_appointment_returns_none_when_doctor_unresolvable(session_maker):
    from web.api import _ensure_relational_appointment

    patient_id = _make_patient(session_maker)
    blob_appt = {
        "appointment_id": str(uuid.uuid4()),
        "patient_user_id": str(patient_id),
        "doctor_id": "no_such_doctor",
        "slot": "2099-06-15T10:00",
    }
    with session_maker() as session:
        result = _ensure_relational_appointment(session, blob_appt)
        assert result is None


# ---------------------------------------------------------------------------
# AppointmentRepository.delete — dismiss-route deletion semantics fix
# ---------------------------------------------------------------------------

def test_appointment_delete_removes_row_and_returns_true(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        assert AppointmentRepository(session).delete(appt_id) is True
        session.commit()

    with session_maker() as session:
        assert AppointmentRepository(session).get_by_id(appt_id) is None


def test_appointment_delete_missing_id_returns_false(session_maker):
    with session_maker() as session:
        assert AppointmentRepository(session).delete(uuid.uuid4()) is False


def test_appointment_delete_cascades_to_sub_entities(session_maker):
    """Deleting an Appointment must cascade to every sub-entity table
    (ondelete="CASCADE" on appointment_id) — the whole shadow tree goes
    with it, matching the blob's own "permanently remove" semantics."""
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        SuggestedTestRepository(session).create(appt_id, test="CBC", category="blood")
        ReferralRepository(session).upsert(appt_id, specialty="Cardiology")
        session.commit()

    with session_maker() as session:
        assert AppointmentRepository(session).delete(appt_id) is True
        session.commit()

    with session_maker() as session:
        assert SuggestedTestRepository(session).list_for_appointment(appt_id) == []
        assert ReferralRepository(session).get_for_appointment(appt_id) is None


# ---------------------------------------------------------------------------
# FileRepository
# ---------------------------------------------------------------------------

def test_file_replace_for_appointment_creates_on_first_call(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        dto = FileRepository(session).replace_for_appointment(
            appt_id, "report.pdf",
            storage_path=f"web/data/files/{appt_id}/report.pdf",
            content_type="application/pdf",
        )
        session.commit()
        assert dto.filename == "report.pdf"
        assert dto.storage_path == f"web/data/files/{appt_id}/report.pdf"

    with session_maker() as session:
        files = FileRepository(session).list_for_appointment(appt_id)
        assert len(files) == 1
        assert files[0].filename == "report.pdf"


def test_file_replace_for_appointment_replaces_not_duplicates(session_maker):
    """Re-uploading the same filename must swap the row, not add a second
    one — mirrors the blob's own dedup-by-filename replace semantics."""
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        FileRepository(session).replace_for_appointment(
            appt_id, "report.pdf",
            storage_path=f"web/data/files/{appt_id}/report.pdf",
            content_type="application/pdf",
        )
        session.commit()

    with session_maker() as session:
        FileRepository(session).replace_for_appointment(
            appt_id, "report.pdf",
            storage_path=f"web/data/files/{appt_id}/report.pdf",
            content_type="image/png",
        )
        session.commit()

    with session_maker() as session:
        files = FileRepository(session).list_for_appointment(appt_id)
        assert len(files) == 1
        assert files[0].content_type == "image/png"


def test_file_replace_for_appointment_with_suggested_test_id(session_maker):
    patient_id = _make_patient(session_maker)
    doctor_id = _make_doctor(session_maker)
    appt_id = _make_appointment(session_maker, patient_id, doctor_id)

    with session_maker() as session:
        test_dto = SuggestedTestRepository(session).create(appt_id, test="CBC", category="blood")
        session.commit()
        suggested_test_id = test_dto.id

    with session_maker() as session:
        dto = FileRepository(session).replace_for_appointment(
            appt_id, "labs.jpg",
            storage_path=f"web/data/files/{appt_id}/labs.jpg",
            content_type="image/jpeg",
            suggested_test_id=suggested_test_id,
        )
        session.commit()
        assert dto.suggested_test_id == suggested_test_id
