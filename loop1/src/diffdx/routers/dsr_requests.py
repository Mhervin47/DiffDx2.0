"""Patient-facing DSR erasure request queue — the in-app front door to the
admin-operated DSR console (admin_portal/routers/dsr.py). A patient can ask
that their data be deleted; an admin reviews the request and either denies
it (here) or approves it by running that console's existing, unmodified
erase flow. This router never deletes anything itself.

AuditLogMiddleware (diffdx.main) only covers an explicit allowlist of path
prefixes, and /api/patient/dsr-request is not one of them — so the create
route below calls log_audit_event() itself, same as every route in
admin_portal/routers/dsr.py already does for the same reason.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select

from diffdx.audit import log_audit_event
from diffdx.db.engine import get_sessionmaker
from diffdx.db.models.dsr import DsrErasureRequest
from diffdx.dependencies import require_role

router = APIRouter(tags=["dsr_requests"])


class DsrRequestCreate(BaseModel):
    reason: str | None = None


def _serialize(req: DsrErasureRequest | None) -> dict[str, Any]:
    if req is None:
        return {"status": "none"}
    return {
        "status": req.status,
        "reason": req.reason,
        "requested_at": req.requested_at.isoformat() if req.requested_at else None,
        "reviewed_at": req.reviewed_at.isoformat() if req.reviewed_at else None,
        "admin_note": req.admin_note,
    }


@router.post("/api/patient/dsr-request")
def create_dsr_request(
    body: DsrRequestCreate, request: Request, patient: dict = Depends(require_role("patient")),
) -> dict[str, Any]:
    uid = uuid.UUID(str(patient["id"]))

    with get_sessionmaker()() as db:
        existing = db.execute(
            select(DsrErasureRequest)
            .where(DsrErasureRequest.user_id == uid, DsrErasureRequest.status == "pending")
        ).scalar_one_or_none()
        if existing is not None:
            result = _serialize(existing)
        else:
            req = DsrErasureRequest(user_id=uid, reason=body.reason)
            db.add(req)
            db.commit()
            db.refresh(req)
            result = _serialize(req)

    log_audit_event(
        actor=patient, action="POST /api/patient/dsr-request", resource_type="user", resource_id=str(uid),
        ip_address=request.client.host if request.client else None,
    )
    return {"status": "ok", "request": result}


@router.get("/api/patient/dsr-request")
def get_dsr_request(patient: dict = Depends(require_role("patient"))) -> dict[str, Any]:
    uid = uuid.UUID(str(patient["id"]))

    with get_sessionmaker()() as db:
        latest = db.execute(
            select(DsrErasureRequest)
            .where(DsrErasureRequest.user_id == uid)
            .order_by(DsrErasureRequest.requested_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    return {"status": "ok", "request": _serialize(latest)}
