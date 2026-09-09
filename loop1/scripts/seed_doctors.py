#!/usr/bin/env python3
"""Seed the 23 demo doctor accounts.

Moved out of web/api.py's startup hook — TASK4_SPLIT_ROUTERS.md §2 flagged
seeding on every boot as "a race in a multi-replica deployment." Run this
once against a fresh database instead: wired into loop1/docker-entrypoint.sh
(after `alembic upgrade head`, before starting uvicorn) and render.yaml's
buildCommand.

Idempotent — same check the original startup hook used
(UserRepository.get_by_doctor_id before creating), so running this twice
against the same database creates 0 doctors on the second run.

Usage:
    PYTHONPATH=src python scripts/seed_doctors.py
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from diffdx.db.engine import get_sessionmaker
from diffdx.legacy_store import _hash_password, _load_doctors, _save_doctors
from diffdx.repositories.users import UserRepository

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger(__name__)

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


def seed_doctor_accounts() -> None:
    with get_sessionmaker()() as db:
        repo = UserRepository(db)
        changed = False
        for seed in _DOCTOR_SEED:
            if repo.get_by_doctor_id(seed["doctor_id"]) is not None:
                continue
            repo.create_doctor(
                name=seed["name"],
                email=seed["email"].lower(),
                password_hash=_hash_password("Doctor123!"),
                doctor_id=seed["doctor_id"],
                specialty=seed["specialty"],
            )
            changed = True
            _log.info("Seeded doctor account: %s (%s)", seed["name"], seed["email"])
        if changed:
            db.commit()
        else:
            _log.info("All %d doctor accounts already exist — nothing to seed.", len(_DOCTOR_SEED))


def _hospital_from_email(email: str) -> str:
    domain = email.split("@", 1)[1].split(".", 1)[0]
    return domain.replace("_", " ").title() + " Clinic"


def _avatar_initials(name: str) -> str:
    words = [w for w in name.replace("Dr.", "").split() if w]
    return "".join(w[0] for w in words[:2]).upper()


def _generate_slots(seed_index: int) -> list[str]:
    """Next 14 days, weekdays only, business hours minus a lunch break —
    enough to make booking immediately testable. Doctors can replace this
    via PATCH /api/doctor/slots once they set their own real availability."""
    hours = [9, 10, 11, 14, 15, 16]
    now = datetime.now(timezone.utc)
    slots: list[str] = []
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(1, 15):
        d = day + timedelta(days=offset)
        if d.weekday() >= 5:  # Sat/Sun
            continue
        for h in hours:
            slots.append(d.replace(hour=h).strftime("%Y-%m-%dT%H:%M"))
    return slots


def seed_doctor_directory() -> None:
    """Seed the legacy blob-store "doctors" directory that /api/doctors
    (patient search) and PATCH /api/doctor/slots (a doctor's own
    availability) actually read/write — separate from the relational
    users/doctors table seed_doctor_accounts() populates above, which is
    auth/login identity only. Without this, a fresh deployment has doctor
    LOGIN accounts but an empty patient-facing directory, and doctors get
    a 404 "Doctor record not found" trying to set their own availability
    (found live: /api/doctors returned zero results on a fresh install).

    Only creates entries for doctor_ids not already present — never
    overwrites an existing entry, so a doctor's own slot customization
    (or an admin edit) survives re-running this script.
    """
    doctors = _load_doctors()
    existing_ids = {d.get("id") for d in doctors}
    changed = False
    for i, seed in enumerate(_DOCTOR_SEED):
        if seed["doctor_id"] in existing_ids:
            continue
        doctors.append({
            "id": seed["doctor_id"],
            "name": seed["name"],
            "specialty": seed["specialty"],
            "hospital": _hospital_from_email(seed["email"]),
            "rating": round(4.3 + (i % 7) * 0.1, 1),
            "avatar_initials": _avatar_initials(seed["name"]),
            "available_slots": _generate_slots(i),
        })
        changed = True
        _log.info("Seeded doctor directory entry: %s (%s)", seed["name"], seed["doctor_id"])
    if changed:
        _save_doctors(doctors)
    else:
        _log.info("All %d doctor directory entries already exist — nothing to seed.", len(_DOCTOR_SEED))


if __name__ == "__main__":
    seed_doctor_accounts()
    seed_doctor_directory()
