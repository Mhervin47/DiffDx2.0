"""/api/messages/* — moved from web/api.py, Task 4's second split-out
router. Logic unchanged from the original; only the location moved and
manual `_get_user_from_request` + 401 checks became
Depends(get_current_user). _load_messages/_save_messages/_is_thread_id
were private to this domain (verified: not referenced anywhere else in
web/api.py) so they moved here too, not just the routes — unlike auth.py's
shared helpers, which stayed behind. They still call the deeper shared
blob-store primitives (_db_load/_db_save) via a lazy import from web.api;
see TASK4_SPLIT_ROUTERS.md §3 for why that's temporary scaffolding.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from diffdx.audit import log_audit_event
from diffdx.db.engine import get_sessionmaker
from diffdx.db.models.reports import MessageReport
from diffdx.dependencies import get_current_user
from diffdx.legacy_store import (
    _db_load,
    _db_save,
    _load_appointments,
)

router = APIRouter(prefix="/api/messages", tags=["messaging"])


class MessageRequest(BaseModel):
    appointment_id: str
    body: str


class MessageReportRequest(BaseModel):
    thread_id: str
    reason: str
    details: str | None = None


_VALID_REPORT_REASONS = {"non_medical", "harassment", "inappropriate_content", "spam", "other"}


def _load_messages() -> list:

    return _db_load("messages", [])


def _save_messages(msgs: list) -> None:

    _db_save("messages", msgs)


def _is_thread_id(s: str) -> bool:
    return "__" in s


# Messaging used to stay open forever on any appointment, effectively an
# unbounded hotline. Kept simple rather than a fixed pre/post-appointment
# clock window (e.g. "6h before, 12h after"): that would directly conflict
# with the post-visit test-results workflow (POST_VISIT_RESULTS_NOTIFICATION_PLAN.md),
# where a patient legitimately uploads results — and may want to message
# about them — days after a visit is marked seen. So instead: messaging
# stays open for the entire life of an "upcoming" appointment (no pre-visit
# restriction), plus a grace period after it's marked "seen"; beyond that,
# a new appointment is required to continue the conversation. Message
# history itself is never hidden by this — only new sends are blocked.
_MESSAGING_GRACE_DAYS = 7


def _messaging_blocked_reason(appt: dict) -> str | None:
    status = appt.get("status", "upcoming")
    if status == "upcoming":
        return None
    if status == "seen":
        # status_updated_at is set on every status transition (see
        # appointments.py's update_appointment_status); fall back to slot
        # for older records that predate that field.
        anchor = appt.get("status_updated_at") or appt.get("slot") or ""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_MESSAGING_GRACE_DAYS)).isoformat()
        if not anchor or anchor >= cutoff:
            return None
        return (
            f"Messaging for this visit closed {_MESSAGING_GRACE_DAYS} days after it was "
            "marked seen. Book a new appointment to continue the conversation."
        )
    return "Messaging isn't available for this appointment. Book a new appointment to start a conversation."


@router.get("")
async def list_messages(user: dict = Depends(get_current_user)):
    """Return message threads grouped by patient+doctor pair (not per appointment)."""

    msgs = _load_messages()
    uid = user["id"]
    role = user.get("role", "patient")
    doctor_id = user.get("doctor_id")
    appointments = _load_appointments()
    threads: dict = {}
    for m in msgs:
        if role == "doctor":
            if m.get("doctor_id") != doctor_id:
                continue
        else:
            if m.get("patient_user_id") != uid:
                continue
        # Key by patient+doctor pair so all appointments share one thread
        p_uid = m.get("patient_user_id", "")
        d_id = m.get("doctor_id", "")
        key = f"{p_uid}__{d_id}"
        appt_id = m.get("appointment_id", "")
        if key not in threads:
            threads[key] = {
                "thread_id": key,
                "latest_appointment_id": appt_id,
                "appointment_ids": [],
                "patient_name": m.get("patient_name", ""),
                "doctor_name": m.get("doctor_name", ""),
                "specialty": "",
                "urgency": "routine",
                "messages": [],
                "unread": 0,
            }
        t = threads[key]
        # Track appointment ids and pick most recent for sending
        if appt_id and appt_id not in t["appointment_ids"]:
            t["appointment_ids"].append(appt_id)
            appt_meta = appointments.get(appt_id, {})
            appt_slot = appt_meta.get("slot", "")
            cur_slot = appointments.get(t["latest_appointment_id"], {}).get("slot", "")
            if appt_slot and (not cur_slot or appt_slot > cur_slot):
                t["latest_appointment_id"] = appt_id
                t["specialty"] = appt_meta.get("specialty", t["specialty"])
                t["urgency"] = appt_meta.get("urgency", t["urgency"])
        t["messages"].append(m)
        if not m.get("read") and m.get("sender_role") != role:
            t["unread"] += 1
    # Sort messages within each thread by time
    for t in threads.values():
        t["messages"].sort(key=lambda m: m.get("sent_at", ""))
    result = sorted(threads.values(), key=lambda t: t["messages"][-1]["sent_at"], reverse=True)
    return {"threads": result}


@router.post("")
async def send_message(req: MessageRequest, user: dict = Depends(get_current_user)):
    """Send a message on an appointment thread."""

    appointments = _load_appointments()
    appt = appointments.get(req.appointment_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    role = user.get("role", "patient")
    if role == "doctor":
        if appt.get("doctor_id") != user.get("doctor_id"):
            raise HTTPException(status_code=403, detail="Not your appointment.")
    else:
        if appt.get("patient_user_id") != user["id"]:
            raise HTTPException(status_code=403, detail="Not your appointment.")
    blocked_reason = _messaging_blocked_reason(appt)
    if blocked_reason:
        raise HTTPException(status_code=403, detail=blocked_reason)
    msg = {
        "message_id": str(uuid.uuid4()),
        "appointment_id": req.appointment_id,
        "patient_user_id": appt.get("patient_user_id", ""),
        "patient_name": appt.get("patient_name", ""),
        "doctor_id": appt.get("doctor_id", ""),
        "doctor_name": appt.get("doctor_name", ""),
        "sender_role": role,
        "sender_name": user.get("name", ""),
        "body": req.body.strip(),
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "read": False,
    }
    msgs = _load_messages()
    msgs.append(msg)
    _save_messages(msgs)
    return {"sent": True, "message": msg}


@router.post("/report")
async def report_thread(req: MessageReportRequest, request: Request, user: dict = Depends(get_current_user)):
    """Flag the other party in a message thread for admin review — e.g. the
    channel being used for something other than medical care. Doesn't
    block, hide, or notify the other party; purely creates a reviewable
    record for admin_portal/routers/reports.py, same relationship the DSR
    erasure request queue has to the admin's actual erase flow."""
    if req.reason not in _VALID_REPORT_REASONS:
        raise HTTPException(status_code=400, detail=f"reason must be one of: {sorted(_VALID_REPORT_REASONS)}")
    if req.reason == "other" and not (req.details or "").strip():
        raise HTTPException(status_code=400, detail="details is required when reason is 'other'.")
    if not _is_thread_id(req.thread_id):
        raise HTTPException(status_code=400, detail="thread_id must be a patient__doctor thread id.")
    p_uid, d_id = req.thread_id.split("__", 1)

    role = user.get("role", "patient")
    if role == "doctor":
        if user.get("doctor_id") != d_id:
            raise HTTPException(status_code=403, detail="Not your conversation.")
    else:
        if user["id"] != p_uid:
            raise HTTPException(status_code=403, detail="Not your conversation.")

    msgs = _load_messages()
    thread_msgs = [m for m in msgs if m.get("patient_user_id") == p_uid and m.get("doctor_id") == d_id]
    if not thread_msgs:
        raise HTTPException(status_code=404, detail="No conversation found to report.")
    sample = thread_msgs[-1]

    if role == "doctor":
        reported_patient_user_id, reported_doctor_id = p_uid, None
        reported_name = sample.get("patient_name", "")
    else:
        reported_patient_user_id, reported_doctor_id = None, d_id
        reported_name = sample.get("doctor_name", "")

    reporter_uuid = uuid.UUID(str(user["id"]))
    with get_sessionmaker()() as db:
        existing = db.execute(
            select(MessageReport).where(
                MessageReport.reporter_user_id == reporter_uuid,
                MessageReport.thread_id == req.thread_id,
                MessageReport.status == "open",
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.reason = req.reason
            existing.details = req.details
            db.commit()
            report_id = str(existing.id)
        else:
            rep = MessageReport(
                reporter_user_id=reporter_uuid,
                reporter_role=role,
                reporter_name=user.get("name", ""),
                thread_id=req.thread_id,
                reported_patient_user_id=reported_patient_user_id,
                reported_doctor_id=reported_doctor_id,
                reported_name=reported_name,
                reason=req.reason,
                details=req.details,
            )
            db.add(rep)
            db.commit()
            db.refresh(rep)
            report_id = str(rep.id)

    log_audit_event(
        actor=user, action="POST /api/messages/report", resource_type="message_report", resource_id=report_id,
        ip_address=request.client.host if request.client else None,
    )
    return {"ok": True, "report_id": report_id}


@router.patch("/{thread_or_appt_id}/read")
async def mark_messages_read(thread_or_appt_id: str, user: dict = Depends(get_current_user)):
    """Mark all messages in a thread as read. Accepts thread_id (uid__docid) or appointment_id."""
    role = user.get("role", "patient")
    msgs = _load_messages()
    if _is_thread_id(thread_or_appt_id):
        p_uid, d_id = thread_or_appt_id.split("__", 1)
        for m in msgs:
            if m.get("patient_user_id") == p_uid and m.get("doctor_id") == d_id:
                if m.get("sender_role") != role:
                    m["read"] = True
    else:
        for m in msgs:
            if m.get("appointment_id") == thread_or_appt_id and m.get("sender_role") != role:
                m["read"] = True
    _save_messages(msgs)
    return {"ok": True}


@router.delete("/{thread_or_appt_id}")
async def delete_conversation(thread_or_appt_id: str, user: dict = Depends(get_current_user)):
    """Delete all messages in a thread. Accepts thread_id (uid__docid) or appointment_id."""
    uid = user["id"]
    role = user.get("role", "patient")
    doctor_id = user.get("doctor_id")
    msgs = _load_messages()
    kept = []
    for m in msgs:
        if _is_thread_id(thread_or_appt_id):
            p_uid, d_id = thread_or_appt_id.split("__", 1)
            belongs = m.get("patient_user_id") == p_uid and m.get("doctor_id") == d_id
        else:
            belongs = m.get("appointment_id") == thread_or_appt_id
        if not belongs:
            kept.append(m)
            continue
        if role == "doctor" and m.get("doctor_id") != doctor_id:
            kept.append(m)
            continue
        if role != "doctor" and m.get("patient_user_id") != uid:
            kept.append(m)
            continue
    _save_messages(kept)
    return {"ok": True}


@router.get("/unread-count")
async def unread_message_count(user: dict = Depends(get_current_user)):
    """Return count of unread messages for the current user."""
    role = user.get("role", "patient")
    doctor_id = user.get("doctor_id")
    uid = user["id"]
    msgs = _load_messages()
    count = 0
    for m in msgs:
        if m.get("read"):
            continue
        if m.get("sender_role") == role:
            continue
        if role == "doctor" and m.get("doctor_id") != doctor_id:
            continue
        if role != "doctor" and m.get("patient_user_id") != uid:
            continue
        count += 1
    return {"count": count}
