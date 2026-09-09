#!/usr/bin/env python3
"""Seed a single admin account — admin portal, Phase 2, Decision Gate D3 Option A.

Reads ADMIN_SEED_EMAIL and ADMIN_SEED_PASSWORD from the environment — never
a literal password in this file. Refuses to run with a clear message if
either is unset.

Correcting the same mistake the Phase 2 brief made in Section 1.10 for the
DSR console (see admin_portal/routers/dsr.py's module docstring for the
full, grep-verified finding): user identity is NOT blob-based.
diffdx.legacy_store._load_users() only ever reads the relational
users/patients tables post-identity-cutover, and store['users'] is never
read or written anywhere in this codebase. So this script writes ONLY the
relational `users` row (via UserRepository.shadow_user — the same bare-row
helper diffdx.audit.log_audit_event already uses for the same reason).
Writing to the blob store too, as the brief's Section 15 literally
suggests, would write to a collection nothing reads: false confidence,
no actual function. login() (routers/auth.py) checks UserRepository.
get_by_email() + the password hash only — no blob lookup, no Patient/
Doctor sub-profile required for a "admin"-role user.

Idempotent — same convention as scripts/seed_doctors.py: if a user with
this email already exists, running this again creates nothing.

Usage:
    ADMIN_SEED_EMAIL=admin@example.com ADMIN_SEED_PASSWORD='...' \\
        PYTHONPATH=src python scripts/seed_admin.py

Not wired into docker-entrypoint.sh or render.yaml — this is a deliberate
manual step (D3 Option A), documented in src/admin_portal/README.md.
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from diffdx.db.engine import get_sessionmaker
from diffdx.legacy_store import _hash_password
from diffdx.repositories.users import UserRepository

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger(__name__)


def seed_admin_account() -> None:
    email = os.environ.get("ADMIN_SEED_EMAIL", "").strip()
    password = os.environ.get("ADMIN_SEED_PASSWORD", "")
    name = os.environ.get("ADMIN_SEED_NAME", "Admin").strip() or "Admin"

    if not email or not password:
        raise SystemExit(
            "ADMIN_SEED_EMAIL and ADMIN_SEED_PASSWORD must both be set in the environment. "
            "Refusing to run without them — there is no default admin password in this codebase."
        )

    with get_sessionmaker()() as db:
        repo = UserRepository(db)
        existing = repo.get_by_email(email)
        if existing is not None:
            _log.info(
                "A user with email %s already exists (role=%s) — nothing to seed.",
                email, existing.role,
            )
            return

        admin_id = uuid.uuid4()
        repo.shadow_user(
            id=admin_id,
            name=name,
            email=email.lower().strip(),
            password_hash=_hash_password(password),
            role="admin",
        )
        db.commit()
        _log.info("Seeded admin account: %s (id=%s)", email, admin_id)


if __name__ == "__main__":
    seed_admin_account()
