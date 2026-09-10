"""
Read-only audit log viewer — admin portal, Phase 2, Task 7.

AuditLogEntry is append-only — no update or delete path exists anywhere in
this codebase (see its own docstring and repositories/audit.py, which
deliberately has no such methods). This router only ever reads it; adding
a mutation endpoint here would be a real regression, not a feature.

actor_name/actor_role are NOT columns on AuditLogEntry (build brief 1.4) —
joined from users via an OUTER join, because actor_user_id is nullable (a
failed login against an unknown email, or a pseudonymised entry after a
DSR erase) and a null actor must still render as "unknown", not drop the
row.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from diffdx.db.engine import get_sessionmaker
from diffdx.db.models.audit import AuditLogEntry
from diffdx.db.models.user import User
from diffdx.dependencies import require_role

router = APIRouter(tags=["admin_portal"])

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _parse_boundary(value: str | None) -> datetime | None:
    """Accepts an ISO8601 string, with or without a timezone offset.
    AuditLogEntry.created_at has no explicit timezone (server_default=
    func.now(), no DateTime(timezone=True)) — a tz-aware input is
    converted to naive UTC so the comparison lines up with how the column
    is actually stored, instead of silently comparing aware-vs-naive
    (which either raises or compares wrong depending on the DB backend)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date: {value!r}")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _row_to_item(row: Any) -> dict[str, Any]:
    entry, actor_name, actor_role = row
    return {
        "id": str(entry.id),
        "ts": entry.created_at.isoformat() if entry.created_at else None,
        "actor_user_id": str(entry.actor_user_id) if entry.actor_user_id else None,
        "actor_name": actor_name or "unknown",
        "actor_role": actor_role or "unknown",
        "action": entry.action,
        "resource_type": entry.resource_type,
        "resource_id": entry.resource_id,
        "ip": entry.ip_address,
    }


def _filtered_query(actor: str | None, action: str | None, resource_type: str | None,
                     from_dt: datetime | None, to_dt: datetime | None):
    stmt = (
        select(AuditLogEntry, User.name, User.role)
        .select_from(AuditLogEntry)
        .outerjoin(User, User.id == AuditLogEntry.actor_user_id)
    )
    if actor:
        stmt = stmt.where(func.lower(User.name).like(f"%{actor.lower()}%"))
    if action:
        stmt = stmt.where(AuditLogEntry.action == action)
    if resource_type:
        stmt = stmt.where(AuditLogEntry.resource_type == resource_type)
    if from_dt:
        stmt = stmt.where(AuditLogEntry.created_at >= from_dt)
    if to_dt:
        stmt = stmt.where(AuditLogEntry.created_at <= to_dt)
    return stmt


@router.get("/api/admin/audit")
def list_audit(
    actor: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _admin: dict = Depends(require_role("admin")),
) -> dict[str, Any]:
    from_dt = _parse_boundary(from_)
    to_dt = _parse_boundary(to)
    stmt = _filtered_query(actor, action, resource_type, from_dt, to_dt)

    with get_sessionmaker()() as db:
        total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
        rows = db.execute(
            stmt.order_by(AuditLogEntry.created_at.desc()).limit(limit).offset(offset)
        ).all()

    return {"status": "ok", "total": total, "items": [_row_to_item(r) for r in rows]}


@router.get("/api/admin/audit/{entry_id}")
def get_audit_entry(entry_id: str, _admin: dict = Depends(require_role("admin"))) -> dict[str, Any]:
    try:
        eid = uuid.UUID(entry_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Audit entry not found.")

    stmt = (
        select(AuditLogEntry, User.name, User.role)
        .select_from(AuditLogEntry)
        .outerjoin(User, User.id == AuditLogEntry.actor_user_id)
        .where(AuditLogEntry.id == eid)
    )
    with get_sessionmaker()() as db:
        row = db.execute(stmt).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Audit entry not found.")
    return {"status": "ok", **_row_to_item(row)}
