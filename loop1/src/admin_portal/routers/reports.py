"""
Message-misuse report review queue — admin portal.

A patient or doctor can flag the other side of a message thread (see
diffdx.routers.messaging's POST /api/messages/report) for using the
channel for something other than medical care. This router is the
admin-side review of that queue — same shape/relationship as
admin_portal/routers/dsr.py is to diffdx.routers.dsr_requests: the
patient/doctor-facing route only ever creates a row; every state change
happens here, and only here.

AuditLogMiddleware does not cover /api/admin/* (see dsr.py's own
docstring for the same finding), so every mutating route here calls
log_audit_event() itself.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from diffdx.audit import log_audit_event
from diffdx.db.engine import get_sessionmaker
from diffdx.db.models.reports import MessageReport
from diffdx.dependencies import require_role

router = APIRouter(tags=["admin_portal"])

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_VALID_STATUSES = {"open", "reviewed", "dismissed"}


class UpdateReportBody(BaseModel):
    status: str
    admin_note: str | None = None


def _row_to_item(r: MessageReport) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "reporter_user_id": str(r.reporter_user_id),
        "reporter_role": r.reporter_role,
        "reporter_name": r.reporter_name,
        "thread_id": r.thread_id,
        "reported_patient_user_id": r.reported_patient_user_id,
        "reported_doctor_id": r.reported_doctor_id,
        "reported_name": r.reported_name,
        "reason": r.reason,
        "details": r.details,
        "status": r.status,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "reviewed_by": str(r.reviewed_by) if r.reviewed_by else None,
        "admin_note": r.admin_note,
    }


def _audit(request: Request | None, admin: dict, action: str, resource_id: str | None) -> None:
    log_audit_event(
        actor=admin, action=action, resource_type="message_report", resource_id=resource_id,
        ip_address=request.client.host if request and request.client else None,
    )


@router.get("/api/admin/message-reports")
def list_message_reports(
    request: Request,
    status: str | None = Query(default="open"),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _admin: dict = Depends(require_role("admin")),
) -> dict[str, Any]:
    # "all" (and, for backward-compat direct API callers, omitting the
    # param entirely / passing None) both mean no status filter.
    if status is not None and status != "all" and status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {sorted(_VALID_STATUSES)} or 'all'")

    stmt = select(MessageReport)
    if status and status != "all":
        stmt = stmt.where(MessageReport.status == status)

    with get_sessionmaker()() as db:
        total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
        rows = db.execute(
            stmt.order_by(MessageReport.created_at.desc()).limit(limit).offset(offset)
        ).scalars().all()

    items = [_row_to_item(r) for r in rows]
    _audit(request, _admin, "GET /api/admin/message-reports", f"{len(items)}_results")
    return {"status": "ok", "total": total, "items": items}


@router.patch("/api/admin/message-reports/{report_id}")
def update_message_report(
    report_id: str, body: UpdateReportBody, request: Request, _admin: dict = Depends(require_role("admin")),
) -> dict[str, Any]:
    if body.status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {sorted(_VALID_STATUSES)}")
    try:
        rid = uuid.UUID(report_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Report not found.")

    with get_sessionmaker()() as db:
        rep = db.get(MessageReport, rid)
        if rep is None:
            raise HTTPException(status_code=404, detail="Report not found.")
        rep.status = body.status
        rep.admin_note = body.admin_note
        rep.reviewed_at = datetime.now(timezone.utc)
        rep.reviewed_by = uuid.UUID(str(_admin["id"]))
        db.commit()

    _audit(request, _admin, f"PATCH /api/admin/message-reports/{{id}} -> {body.status}", report_id)
    return {"status": "ok"}
