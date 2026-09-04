"""/api/session/{id}/suggested-test-files — the two routes left over from
the sessions.py/session_booking.py split (they sat ~1000 lines away from
the rest of that domain, interleaved with the doctor/patient appointment
routes — see TASK4_SPLIT_ROUTERS.md §1a). Task 4's seventh split-out
router. Logic unchanged from the original.

_MAX_FILE_BYTES and _session_test_uploads are genuine shared state — the
latter is a module-level dict also mutated by
routers/session_booking.py's book_appointment (imported lazily there
too) and by not-yet-split doctor/patient file-upload routes. Left in
web.api, lazily imported, same as every other still-shared helper.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from diffdx.legacy_store import (
    _MAX_FILE_BYTES,
    _get_user_from_request,
    _load_appointments,
    _save_appointments,
    _save_file_data,
    _save_session_uploads,
    _session_test_uploads,
)

router = APIRouter(tags=["sessions"])


@router.post("/api/session/{session_id}/suggested-test-files")
async def upload_suggested_test_file(
    session_id: str, request: Request,
    file: UploadFile = File(...),
    suggested_test_id: str | None = None,
    suggested_test_name: str | None = None,
):
    """Upload a result file for an AI-suggested test. Works with or without a booked appointment."""

    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    _ALLOWED = {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp", "image/heic"}
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED:
        raise HTTPException(status_code=415, detail="Only PDF and image files are allowed.")
    raw = await file.read()
    if len(raw) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB).")

    data_b64 = base64.b64encode(raw).decode("ascii")
    record = {
        "filename": file.filename,
        "size_bytes": len(raw),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "mime_type": file.content_type or "application/octet-stream",
        "suggested_test_id": suggested_test_id or None,
        "suggested_test_name": suggested_test_name or None,
        "session_id": session_id,
        "user_id": user["id"],
    }

    # Store binary data separately (not inline in appointments blob)
    _save_file_data(f"session:{session_id}", file.filename, data_b64)

    # Store metadata-only record in session uploads bucket
    bucket = _session_test_uploads.setdefault(session_id, {})
    tid = suggested_test_id or file.filename
    bucket[tid] = record
    _save_session_uploads(_session_test_uploads)

    # Also attach to matching appointment if one exists
    appointments = _load_appointments()
    for appt in appointments.values():
        if appt.get("session_id") == session_id and appt.get("patient_user_id") == user["id"]:
            appt_id = appt["appointment_id"]
            _save_file_data(appt_id, file.filename, data_b64)
            files = appt.setdefault("patient_files", [])
            files[:] = [f for f in files if f.get("filename") != file.filename]
            files.append(record)
            if suggested_test_id:
                appt.setdefault("suggested_test_uploads", {})[suggested_test_id] = {
                    "filename": file.filename,
                    "uploaded_at": record["uploaded_at"],
                    "test_name": suggested_test_name or suggested_test_id,
                }
            _save_appointments(appointments)
            break

    return {"saved": True, "filename": file.filename, "size_bytes": len(raw)}


@router.get("/api/session/{session_id}/suggested-test-files")
async def list_suggested_test_files(session_id: str, request: Request):
    """Return already-uploaded suggested test files for this session."""

    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    bucket = _session_test_uploads.get(session_id, {})
    return {
        "uploads": {
            tid: {k: v for k, v in rec.items() if k != "data_b64"}
            for tid, rec in bucket.items()
            if rec.get("user_id") == user["id"]
        }
    }
