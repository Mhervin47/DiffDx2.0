"""Single entry point for writing audit log events (Task 5).

Call sites pass the blob-store user dict they already have (or None, e.g.
a failed login against an email that matched no account) — this module
handles opening a short-lived DB session, upserting the Task 5 "shadow
user" row so the FK on audit_log_entries.actor_user_id is satisfiable
(see repositories.users.UserRepository.shadow_user's docstring for why
that's needed), writing the entry, and committing.

Never raises. An audit-write failure (DB unreachable, etc.) is logged and
swallowed rather than breaking the request it's attached to — matching
this codebase's existing "never block the main flow" pattern for
non-critical side effects (see web.api._send_email_notification).
"""
from __future__ import annotations

import logging
import uuid

from diffdx.db.engine import get_sessionmaker
from diffdx.repositories.audit import write_audit_log_entry
from diffdx.repositories.users import UserRepository

_log = logging.getLogger(__name__)


def log_audit_event(
    *,
    actor: dict | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    ip_address: str | None = None,
) -> None:
    session = get_sessionmaker()()
    try:
        actor_user_id: uuid.UUID | None = None
        if actor is not None:
            try:
                actor_user_id = uuid.UUID(str(actor["id"]))
            except (KeyError, ValueError):
                actor_user_id = None
            if actor_user_id is not None:
                UserRepository(session).shadow_user(
                    id=actor_user_id,
                    name=actor.get("name", ""),
                    email=actor.get("email", ""),
                    password_hash=actor.get("password_hash", ""),
                    role=actor.get("role", "patient"),
                )
        write_audit_log_entry(
            session,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip_address,
        )
        session.commit()
    except Exception:
        session.rollback()
        _log.warning("Audit log write failed for action=%s", action, exc_info=True)
    finally:
        session.close()
