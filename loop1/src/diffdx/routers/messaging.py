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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

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


def _load_messages() -> list:

    return _db_load("messages", [])


def _save_messages(msgs: list) -> None:

    _db_save("messages", msgs)


def _is_thread_id(s: str) -> bool:
    return "__" in s


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
