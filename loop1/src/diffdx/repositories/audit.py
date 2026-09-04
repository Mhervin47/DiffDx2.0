"""Append-only audit log writes (Task 5). No update/delete methods exist
here on purpose — see diffdx.db.models.audit.AuditLogEntry's docstring."""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from diffdx.db.models.audit import AuditLogEntry


def write_audit_log_entry(
    session: Session,
    *,
    actor_user_id: uuid.UUID | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    ip_address: str | None = None,
) -> None:
    # action/resource_type/resource_id are String(100) columns — SQLite
    # won't enforce that, but Postgres will, and callers pass through
    # user-controlled values in places (a filename in a download URL, an
    # attempted login email) that could exceed it. Truncate here, once,
    # rather than at every call site — losing the last few characters of
    # an audit value is far better than losing the whole entry to a
    # silently-swallowed insert failure (see diffdx.audit.log_audit_event).
    session.add(AuditLogEntry(
        id=uuid.uuid4(),
        actor_user_id=actor_user_id,
        action=action[:100],
        resource_type=resource_type[:100] if resource_type else None,
        resource_id=resource_id[:100] if resource_id else None,
        ip_address=ip_address,
    ))
    session.flush()
