#!/usr/bin/env python3
"""Migrate the legacy JSON-blob `store` table into the relational schema.

Usage:
    cd loop1
    PYTHONPATH=src:. .venv/bin/python scripts/migrate_blob_to_relational.py --dry-run
    PYTHONPATH=src:. .venv/bin/python scripts/migrate_blob_to_relational.py

DATABASE_URL selects both the source (old blob `store` table) and the
destination (new relational schema) — they're the same database, this is a
same-DB migration. Run `alembic upgrade head` first.

--dry-run runs the *real* migration logic (including doctor/patient id
resolution against rows just written earlier in the same pass) inside a
transaction, then rolls it back instead of committing, and skips writing
extracted files to disk. This is what makes dry-run counts accurate for
things like appointments/waitlist/messages that depend on doctors already
being resolved earlier in the same run — a version that skipped DB queries
entirely in dry-run mode couldn't resolve those and reported false conflicts.

Idempotent: every row preserves its original id from the blob (or a
deterministic uuid5 hash for blob records that never had one — blocked
dates, doctor slots), so re-running skips anything already migrated rather
than duplicating it.

Known, deliberate scope limits (see TASK2_REPOSITORY_LAYER.md):
  - `session_uploads` (pre-booking upload staging) is transient scratch
    state, not migrated.
  - `prescription_history` (batch history) is not migrated — only the
    current/latest `appointments[*]["prescriptions"]` list is, as
    individual Prescription rows.
  - Diagnostic sessions are migrated from `session_report:{id}` blob keys
    only. Sessions that only ever wrote to logs/final_records/ on disk and
    were never fetched into the DB report cache are out of scope for this
    pass.
  - Misc doctor-added appointment fields not covered by the Task 1 schema
    (tags, doctor_summary, follow_up linkage beyond what's already on
    Appointment) are not migrated — nothing in the new schema has a slot
    for them yet.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from diffdx.db import models  # noqa: F401 — registers all models
from diffdx.db.engine import get_engine, get_sessionmaker
from diffdx.db.models.clinical import (
    DiagnosticSession,
    Prescription,
    Referral,
    SecondOpinion,
    SessionTurn,
    SuggestedTest,
    TreatmentPlanItem,
)
from diffdx.db.models.files import UploadedFile
from diffdx.db.models.messaging import Message, MessageThread
from diffdx.db.models.scheduling import Appointment, BlockedDate, DoctorSlot, Waitlist
from diffdx.db.models.user import Dependent, Doctor, Patient, User

_REPO_ROOT = Path(__file__).parent.parent
_FILES_DIR = _REPO_ROOT / "web" / "data" / "files"

# Deterministic namespace for records that never had an id in the blob store.
_UUID_NS = uuid.UUID("d1ffd000-0000-0000-0000-000000000001")


def _uuid5(*parts: str) -> uuid.UUID:
    return uuid.uuid5(_UUID_NS, "|".join(parts))


def _parse_uuid(s: str | None) -> uuid.UUID | None:
    if not s:
        return None
    try:
        return uuid.UUID(s)
    except (ValueError, AttributeError):
        return None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _doctor_user_id(session: Session, doctor_id_str: str | None) -> uuid.UUID | None:
    if not doctor_id_str:
        return None
    row = session.execute(
        text("SELECT user_id FROM doctors WHERE doctor_id = :did"), {"did": doctor_id_str}
    ).fetchone()
    return row[0] if row else None


@dataclass
class Counters:
    read: dict[str, int] = field(default_factory=dict)
    written: dict[str, int] = field(default_factory=dict)
    skipped_existing: dict[str, int] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)

    def bump_read(self, table: str, n: int = 1) -> None:
        self.read[table] = self.read.get(table, 0) + n

    def bump_written(self, table: str, n: int = 1) -> None:
        self.written[table] = self.written.get(table, 0) + n

    def bump_skipped(self, table: str, n: int = 1) -> None:
        self.skipped_existing[table] = self.skipped_existing.get(table, 0) + n


def load_blob_store() -> dict[str, Any]:
    """Read the entire legacy `store` table into memory as {collection: data}."""
    engine = get_engine()
    with engine.connect() as conn:
        try:
            rows = conn.execute(text("SELECT collection, data FROM store")).fetchall()
        except Exception:
            # store table doesn't exist yet (fresh DB, old app never ran) — nothing to migrate.
            return {}
    return {collection: json.loads(data) for collection, data in rows}


# ── Users, Patients, Doctors, Dependents ────────────────────────────────────

def migrate_users_and_doctors(session: Session, store: dict, counters: Counters) -> None:
    users_blob: dict = store.get("users", {})
    doctors_blob: list = store.get("doctors", [])
    counters.bump_read("users", len(users_blob))
    counters.bump_read("doctors_directory", len(doctors_blob))

    doctors_by_id = {d["id"]: d for d in doctors_blob}

    for user_id_str, u in users_blob.items():
        user_id = _parse_uuid(user_id_str) or _parse_uuid(u.get("id"))
        if user_id is None:
            counters.conflicts.append(f"users: unparseable id {user_id_str!r}, skipped")
            continue

        role = u.get("role", "patient")
        sub_row_model = Doctor if role == "doctor" else Patient
        existing_user = session.get(User, user_id)
        sub_row_exists = session.get(sub_row_model, user_id) is not None

        # Idempotency must key off the *role-appropriate sub-row*, not just the
        # bare `User` row — Task 5's shadow_user() already writes a bare User
        # row on every login, with no Patient/Doctor sub-row attached. Gating
        # only on `User` existence would see that shadow row, decide "already
        # migrated", and skip — permanently leaving that user without a
        # profile/doctor row once reads flip to relational.
        if existing_user is not None and sub_row_exists:
            counters.bump_skipped("users")
        else:
            if existing_user is None:
                user = User(
                    id=user_id,
                    name=u.get("name", ""),
                    email=(u.get("email") or "").lower().strip(),
                    password_hash=u.get("password_hash", ""),
                    role=role,
                )
                created_at = _parse_dt(u.get("created_at"))
                if created_at:
                    user.created_at = created_at
                session.add(user)
                session.flush()
                counters.bump_written("users")
            else:
                # Bare shadow row already exists — the blob is still the
                # source of truth pre-cutover, so sync it in rather than
                # trusting a shadow row written from a possibly-stale login.
                existing_user.name = u.get("name") or existing_user.name
                existing_user.email = (u.get("email") or existing_user.email or "").lower().strip()
                existing_user.password_hash = u.get("password_hash") or existing_user.password_hash
                existing_user.role = role
                created_at = _parse_dt(u.get("created_at"))
                if created_at:
                    existing_user.created_at = created_at
                session.flush()

            if not sub_row_exists:
                if role == "doctor":
                    directory = doctors_by_id.get(u.get("doctor_id"), {})
                    session.add(Doctor(
                        user_id=user_id,
                        doctor_id=u.get("doctor_id", ""),
                        specialty=u.get("specialty") or directory.get("specialty", "General"),
                        hospital=directory.get("hospital"),
                        rating=directory.get("rating"),
                        avatar_initials=directory.get("avatar_initials"),
                    ))
                    counters.bump_written("doctors")
                else:
                    session.add(Patient(
                        user_id=user_id,
                        mobile=u.get("mobile"),
                        age=u.get("age"),
                        blood_type=u.get("blood_type"),
                        gender=u.get("gender"),
                        address=u.get("address"),
                        emergency_contact_name=u.get("emergency_contact_name"),
                        emergency_contact_phone=u.get("emergency_contact_phone"),
                        allergies=u.get("allergies"),
                        chronic_conditions=u.get("chronic_conditions"),
                    ))
                    counters.bump_written("patients")
                session.flush()
            else:
                counters.bump_skipped(role + "s")

        # Dependents (nested list, patients only)
        for dep in u.get("dependents", []):
            dep_id = _parse_uuid(dep.get("id")) or _uuid5("dependent", user_id_str, dep.get("name", ""))
            counters.bump_read("dependents")
            if session.get(Dependent, dep_id) is not None:
                counters.bump_skipped("dependents")
                continue
            session.add(Dependent(
                id=dep_id,
                patient_id=user_id,
                name=dep.get("name", ""),
                relationship_=dep.get("relationship", "other"),
                age=dep.get("age"),
                gender=dep.get("gender"),
                blood_type=dep.get("blood_type"),
                allergies=dep.get("allergies"),
                chronic_conditions=dep.get("chronic_conditions"),
            ))
            counters.bump_written("dependents")

    session.flush()

    # Doctor slots (available_slots on the directory record — only meaningful
    # once the Doctor row exists, so a second pass after the loop above).
    for d in doctors_blob:
        doctor_id_str = d.get("id")
        doctor_user_id = _doctor_user_id(session, doctor_id_str)
        for slot_str in d.get("available_slots", []):
            counters.bump_read("doctor_slots")
            slot_dt = _parse_dt(slot_str)
            if doctor_user_id is None:
                counters.conflicts.append(
                    f"doctor_slots: no migrated Doctor row for doctor_id={doctor_id_str!r}"
                )
                continue
            if slot_dt is None:
                counters.conflicts.append(f"doctor_slots: unparseable slot {slot_str!r}, skipped")
                continue
            slot_id = _uuid5("doctor_slot", doctor_id_str, slot_str)
            if session.get(DoctorSlot, slot_id) is not None:
                counters.bump_skipped("doctor_slots")
                continue
            session.add(DoctorSlot(id=slot_id, doctor_id=doctor_user_id, slot_datetime=slot_dt))
            counters.bump_written("doctor_slots")

    session.flush()


# ── Appointments + their sub-entities ───────────────────────────────────────

def _extract_file(store: dict, appt_id: str, filename: str, dry_run: bool) -> str | None:
    """Base64-decode a legacy `file:{appt_id}:{filename}` blob to disk, return its path.

    In dry-run mode, resolves and reports the path without writing bytes.
    """
    key = f"file:{appt_id}:{filename}"
    rec = store.get(key)
    if not rec or "data_b64" not in rec:
        return None
    dest_dir = _FILES_DIR / appt_id
    dest_path = dest_dir / filename
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if not dest_path.exists():
            dest_path.write_bytes(base64.b64decode(rec["data_b64"]))
    return str(dest_path.relative_to(_REPO_ROOT))


def migrate_appointments(session: Session, store: dict, counters: Counters, dry_run: bool) -> None:
    appts_blob: dict = store.get("appointments", {})
    counters.bump_read("appointments", len(appts_blob))

    for appt_id_str, a in appts_blob.items():
        appt_id = _parse_uuid(appt_id_str) or _parse_uuid(a.get("appointment_id"))
        if appt_id is None:
            counters.conflicts.append(f"appointments: unparseable id {appt_id_str!r}, skipped")
            continue

        patient_id = _parse_uuid(a.get("patient_user_id"))
        doctor_user_id = _doctor_user_id(session, a.get("doctor_id"))
        slot_dt = _parse_dt(a.get("slot"))

        if patient_id is None or doctor_user_id is None or slot_dt is None:
            counters.conflicts.append(
                f"appointments {appt_id_str}: missing/unresolvable patient_id, doctor_id, or slot — skipped"
            )
            continue

        already_existed = session.get(Appointment, appt_id) is not None
        if already_existed:
            counters.bump_skipped("appointments")
        else:
            rating = a.get("rating") or {}
            appt = Appointment(
                id=appt_id,
                session_id=a.get("session_id"),
                patient_id=patient_id,
                doctor_id=doctor_user_id,
                slot_datetime=slot_dt,
                status=a.get("status", "upcoming"),
                urgency=(a.get("urgency") or "routine").lower(),
                note=a.get("note"),
                is_followup=bool(a.get("is_followup", False)),
                chief_complaint=a.get("chief_complaint"),
                primary_diagnosis=a.get("primary_diagnosis"),
                patient_age=a.get("age"),
                patient_sex=a.get("sex"),
                patient_bmi=a.get("bmi"),
                cancelled_by=a.get("cancelled_by"),
                rating_stars=rating.get("stars"),
                rating_comment=rating.get("comment"),
            )
            booked_at = _parse_dt(a.get("booked_at"))
            if booked_at:
                appt.booked_at = booked_at
            cancelled_at = _parse_dt(a.get("cancelled_at"))
            if cancelled_at:
                appt.cancelled_at = cancelled_at
            rating_at = _parse_dt(rating.get("submitted_at"))
            if rating_at:
                appt.rating_submitted_at = rating_at
            try:
                session.add(appt)
                session.flush()
            except IntegrityError as exc:
                session.rollback()
                counters.conflicts.append(
                    f"appointments {appt_id_str}: IntegrityError ({exc.orig}) — likely a real "
                    f"double-booking inherited from the old blob store's lack of constraints"
                )
                continue
            counters.bump_written("appointments")

        results_by_test_id = {r["test_id"]: r for r in a.get("test_results_data", []) if r.get("test_id")}
        for t in a.get("test_orders", []):
            counters.bump_read("suggested_tests")
            test_id = _parse_uuid(t.get("id")) or _uuid5("suggested_test", appt_id_str, t.get("id", ""))
            if session.get(SuggestedTest, test_id) is not None:
                counters.bump_skipped("suggested_tests")
                continue
            result = results_by_test_id.get(t.get("id"), {})
            session.add(SuggestedTest(
                id=test_id,
                appointment_id=appt_id,
                test=t.get("test", ""),
                category=t.get("category", "other"),
                priority=t.get("priority", "routine"),
                notes=t.get("notes"),
                result=result.get("result"),
                result_status=result.get("status") if result else None,
                result_recorded_at=_parse_dt(result.get("recorded_at")) if result else None,
            ))
            counters.bump_written("suggested_tests")

        for p in a.get("prescriptions", []):
            counters.bump_read("prescriptions")
            rx_id = _parse_uuid(p.get("id")) or _uuid5("prescription", appt_id_str, p.get("id", ""))
            if session.get(Prescription, rx_id) is not None:
                counters.bump_skipped("prescriptions")
                continue
            session.add(Prescription(
                id=rx_id,
                appointment_id=appt_id,
                drug=p.get("drug", ""),
                dose=p.get("dose", ""),
                route=p.get("route", "oral"),
                frequency=p.get("frequency", ""),
                duration=p.get("duration", ""),
                meal_timing=p.get("meal_timing"),
                notes=p.get("notes"),
            ))
            counters.bump_written("prescriptions")

        ref = a.get("referral")
        if ref:
            counters.bump_read("referrals")
            ref_id = _uuid5("referral", appt_id_str)
            if session.get(Referral, ref_id) is not None:
                counters.bump_skipped("referrals")
            else:
                session.add(Referral(
                    id=ref_id,
                    appointment_id=appt_id,
                    specialty=ref.get("specialty", ""),
                    to_doctor=ref.get("to_doctor"),
                    urgency=ref.get("urgency", "Routine"),
                    notes=ref.get("notes"),
                    internal_note=ref.get("internal_note"),
                ))
                counters.bump_written("referrals")

        for i, item in enumerate(a.get("approved_plan", [])):
            counters.bump_read("treatment_plan_items")
            item_id = _parse_uuid(item.get("id")) or _uuid5("plan_item", appt_id_str, str(i))
            if session.get(TreatmentPlanItem, item_id) is not None:
                counters.bump_skipped("treatment_plan_items")
                continue
            session.add(TreatmentPlanItem(
                id=item_id,
                appointment_id=appt_id,
                text=item.get("text", ""),
                approved=bool(item.get("approved", True)),
                source=item.get("source", "ai"),
            ))
            counters.bump_written("treatment_plan_items")

        for pf in a.get("patient_files", []):
            counters.bump_read("uploaded_files")
            filename = pf.get("filename")
            if not filename:
                continue
            file_id = _uuid5("uploaded_file", appt_id_str, filename)
            if session.get(UploadedFile, file_id) is not None:
                counters.bump_skipped("uploaded_files")
                continue
            storage_path = _extract_file(store, appt_id_str, filename, dry_run)
            if storage_path is None:
                counters.conflicts.append(
                    f"uploaded_files {appt_id_str}/{filename}: no matching file:*  blob, skipped"
                )
                continue
            session.add(UploadedFile(
                id=file_id,
                appointment_id=appt_id,
                filename=filename,
                storage_path=storage_path,
                uploaded_at=_parse_dt(pf.get("uploaded_at")) or datetime.now(timezone.utc),
            ))
            counters.bump_written("uploaded_files")

        session.flush()


# ── Waitlist, blocked dates, second opinions, messages ──────────────────────

def migrate_waitlist(session: Session, store: dict, counters: Counters) -> None:
    entries: list = store.get("waitlist", [])
    counters.bump_read("waitlist", len(entries))
    for e in entries:
        wid = _parse_uuid(e.get("id"))
        if wid is None:
            counters.conflicts.append("waitlist: unparseable id, skipped")
            continue
        if session.get(Waitlist, wid) is not None:
            counters.bump_skipped("waitlist")
            continue
        patient_id = _parse_uuid(e.get("patient_user_id"))
        doctor_user_id = _doctor_user_id(session, e.get("doctor_id"))
        if patient_id is None or doctor_user_id is None:
            counters.conflicts.append(f"waitlist {e.get('id')}: unresolvable patient/doctor, skipped")
            continue
        session.add(Waitlist(
            id=wid, patient_id=patient_id, doctor_id=doctor_user_id,
            note=e.get("note"), status=e.get("status", "waiting"),
        ))
        counters.bump_written("waitlist")
    session.flush()


def migrate_blocked_dates(session: Session, store: dict, counters: Counters) -> None:
    by_doctor: dict = store.get("blocked_dates", {})
    total = sum(len(v) for v in by_doctor.values())
    counters.bump_read("blocked_dates", total)
    for doctor_id_str, entries in by_doctor.items():
        doctor_user_id = _doctor_user_id(session, doctor_id_str)
        for e in entries:
            date_str = e.get("date", "")
            bid = _uuid5("blocked_date", doctor_id_str, date_str)
            if session.get(BlockedDate, bid) is not None:
                counters.bump_skipped("blocked_dates")
                continue
            if doctor_user_id is None:
                counters.conflicts.append(f"blocked_dates: no Doctor row for {doctor_id_str!r}")
                continue
            session.add(BlockedDate(id=bid, doctor_id=doctor_user_id, date=date_str, reason=e.get("reason")))
            counters.bump_written("blocked_dates")
    session.flush()


def migrate_second_opinions(session: Session, store: dict, counters: Counters) -> None:
    opinions: list = store.get("second_opinions", [])
    counters.bump_read("second_opinions", len(opinions))
    for o in opinions:
        oid = _parse_uuid(o.get("id"))
        appt_id = _parse_uuid(o.get("appointment_id"))
        if oid is None or appt_id is None:
            counters.conflicts.append("second_opinions: unparseable id/appointment_id, skipped")
            continue
        if session.get(SecondOpinion, oid) is not None:
            counters.bump_skipped("second_opinions")
            continue
        from_doctor_user_id = _doctor_user_id(session, o.get("from_doctor_id"))
        to_doctor_user_id = _doctor_user_id(session, o.get("to_doctor_id"))
        if from_doctor_user_id is None or to_doctor_user_id is None:
            counters.conflicts.append(f"second_opinions {o.get('id')}: unresolvable doctor(s), skipped")
            continue
        if session.get(Appointment, appt_id) is None:
            counters.conflicts.append(f"second_opinions {o.get('id')}: appointment {appt_id} not migrated, skipped")
            continue
        session.add(SecondOpinion(
            id=oid, appointment_id=appt_id,
            from_doctor_id=from_doctor_user_id, to_doctor_id=to_doctor_user_id,
            patient_summary=o.get("patient_summary"), note=o.get("note"),
            status=o.get("status", "pending"), response=o.get("response") or None,
        ))
        counters.bump_written("second_opinions")
    session.flush()


def migrate_messages(session: Session, store: dict, counters: Counters) -> None:
    msgs: list = store.get("messages", [])
    counters.bump_read("messages", len(msgs))
    thread_cache: dict[tuple, uuid.UUID] = {}
    for m in msgs:
        mid = _parse_uuid(m.get("message_id"))
        if mid is None:
            counters.conflicts.append("messages: unparseable message_id, skipped")
            continue
        if session.get(Message, mid) is not None:
            counters.bump_skipped("messages")
            continue
        patient_id = _parse_uuid(m.get("patient_user_id"))
        doctor_user_id = _doctor_user_id(session, m.get("doctor_id"))
        if patient_id is None or doctor_user_id is None:
            counters.conflicts.append(f"messages {m.get('message_id')}: unresolvable patient/doctor, skipped")
            continue
        thread_key = (patient_id, doctor_user_id)
        thread_id = thread_cache.get(thread_key)
        if thread_id is None:
            thread_id = _uuid5("message_thread", str(patient_id), str(doctor_user_id))
            thread_cache[thread_key] = thread_id
            if session.get(MessageThread, thread_id) is None:
                session.add(MessageThread(id=thread_id, patient_id=patient_id, doctor_id=doctor_user_id))
                session.flush()
        appt_id = _parse_uuid(m.get("appointment_id"))
        msg = Message(
            id=mid, thread_id=thread_id, appointment_id=appt_id,
            sender_role=m.get("sender_role", "patient"), body=m.get("body", ""),
            read=bool(m.get("read", False)),
        )
        sent_at = _parse_dt(m.get("sent_at"))
        if sent_at:
            msg.sent_at = sent_at
        session.add(msg)
        counters.bump_written("messages")
    session.flush()


# ── Diagnostic sessions (from session_report:{id} blob keys) ───────────────

def migrate_diagnostic_sessions(session: Session, store: dict, counters: Counters) -> None:
    report_keys = [k for k in store if k.startswith("session_report:")]
    counters.bump_read("diagnostic_sessions", len(report_keys))
    for key in report_keys:
        session_id_str = key.split(":", 1)[1]
        session_id = _parse_uuid(session_id_str)
        if session_id is None:
            counters.conflicts.append(f"diagnostic_sessions: unparseable session_id {session_id_str!r}, skipped")
            continue
        if session.get(DiagnosticSession, session_id) is not None:
            counters.bump_skipped("diagnostic_sessions")
            continue
        report = store[key]
        patient = report.get("patient", {})
        ds = DiagnosticSession(
            id=session_id,
            chief_complaint=patient.get("chief_complaint"),
            primary_diagnosis=report.get("final_diagnosis"),
            termination_reason=report.get("termination_reason"),
            started_at=_parse_dt(report.get("started_at")),
            ended_at=_parse_dt(report.get("ended_at")),
            total_turns=report.get("total_turns"),
            final_differential=report.get("final_differential"),
            closing_turn=report.get("closing_turn"),
        )
        session.add(ds)
        session.flush()
        for t in report.get("turn_history", []):
            turn_id = _uuid5("session_turn", session_id_str, str(t.get("turn_index", 0)))
            counters.bump_read("session_turns")
            if session.get(SessionTurn, turn_id) is not None:
                counters.bump_skipped("session_turns")
                continue
            session.add(SessionTurn(
                id=turn_id, session_id=session_id, turn_index=t.get("turn_index", 0),
                question=t.get("question"), rationale=t.get("rationale"),
                biggest_uncertainty=t.get("biggest_uncertainty"), patient_answer=t.get("patient_answer"),
                confidence=t.get("confidence"), differential=t.get("differential"),
                doctor_output=t.get("doctor_output"),
            ))
            counters.bump_written("session_turns")
        counters.bump_written("diagnostic_sessions")
    session.flush()


# ── Driver ───────────────────────────────────────────────────────────────────

def run(dry_run: bool) -> int:
    store = load_blob_store()
    if not store:
        print("store table is empty or doesn't exist — nothing to migrate.")
        return 0

    counters = Counters()
    SessionLocal = get_sessionmaker()
    with SessionLocal() as session:
        migrate_users_and_doctors(session, store, counters)
        migrate_appointments(session, store, counters, dry_run)
        migrate_waitlist(session, store, counters)
        migrate_blocked_dates(session, store, counters)
        migrate_second_opinions(session, store, counters)
        migrate_messages(session, store, counters)
        migrate_diagnostic_sessions(session, store, counters)

        if dry_run:
            session.rollback()
        else:
            session.commit()

    print(f"\n{'DRY RUN — ' if dry_run else ''}Reconciliation summary")
    print(f"{'table':<28}{'read':>8}{'written':>10}{'skipped':>10}")
    tables = sorted(set(counters.read) | set(counters.written) | set(counters.skipped_existing))
    for t in tables:
        print(f"{t:<28}{counters.read.get(t, 0):>8}{counters.written.get(t, 0):>10}{counters.skipped_existing.get(t, 0):>10}")

    if counters.conflicts:
        print(f"\n{len(counters.conflicts)} unresolved record(s):")
        for c in counters.conflicts:
            print(f"  - {c}")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing anything.")
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
