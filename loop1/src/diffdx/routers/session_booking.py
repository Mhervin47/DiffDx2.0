"""/api/session/{id}/suggested-tests and /api/session/{id}/book — split
out of routers/sessions.py to keep both files under the 400-line limit.
Same domain (Task 4's third router), same rules: logic unchanged from the
original web/api.py, shared helpers imported lazily. See
routers/sessions.py's module docstring for the full domain context.

book_appointment in particular is the exact code Task 3's concurrency
demo (scripts/concurrency_demo.py, docs/evidence/concurrency.txt) proved
loses bookings under concurrent load — that's expected and unchanged here;
fixing it is Task 4's router-split concern only insofar as *where the code
lives*, not *what it does*. The actual fix is wiring this route through
diffdx.repositories.appointments (Task 2) once enough of the surrounding
domain has moved to make that safe — not yet.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from diffdx.routers.sessions import _suggested_tests_cache
from diffdx.schemas.sessions import BookRequest

router = APIRouter(tags=["sessions"])


@router.get("/api/session/{session_id}/suggested-tests")
async def get_suggested_tests(session_id: str, refresh: bool = False):
    """
    Use the LLM to suggest basic pre-diagnosis tests based on the session's
    final differential. Results are cached per session so repeated calls are free.
    Pass ?refresh=1 to bust the cache.
    """
    from web.api import _get_final_differential, _log, _repo_root

    if not refresh and session_id in _suggested_tests_cache:
        return _suggested_tests_cache[session_id]

    # Load session data
    diff, confidence = _get_final_differential(session_id)
    if diff is None:
        raise HTTPException(status_code=404, detail="Session not found or not complete.")

    # Also pull chief complaint + demographics from final record
    final_path = _repo_root / "logs" / "final_records" / f"final_{session_id}.json"
    chief_complaint = ""
    age = None
    sex = None
    symptoms_text = ""
    if final_path.exists():
        try:
            rec = json.loads(final_path.read_text(encoding="utf-8"))
            fp = rec.get("final_profile", {})
            chief_complaint = fp.get("chief_complaint", "")
            demo = fp.get("demographics", {})
            age = demo.get("age")
            sex = demo.get("sex")
            # Summarise symptoms for LLM context
            syms = fp.get("symptoms", [])
            if syms:
                symptoms_text = "; ".join(
                    f"{s.get('name','')} (onset: {s.get('onset','unknown')}, severity: {s.get('severity','unknown')})"
                    for s in syms[:6]
                )
        except Exception:
            pass

    top_dx = diff[0][0] if diff else "unspecified"
    diff_summary = ", ".join(f"{dx} ({round(p*100)}%)" for dx, p in diff[:4])
    patient_line = f"Patient: {age or '?'} y/o {sex or 'unknown sex'}." if age or sex else ""

    # Scan conversation for any test/investigation mentions
    doctor_test_mentions = ""
    try:
        if final_path.exists():
            rec = json.loads(final_path.read_text(encoding="utf-8"))
            turns = rec.get("turn_history", [])
            # Collect all doctor questions that mention tests, scans, labs, or imaging
            _test_keywords = ("x-ray", "xray", "blood test", "ecg", "ekg", "ultrasound",
                              "scan", "test", "lab", "urine", "chest", "mri", "ct ",
                              "sputum", "spirometry", "biopsy", "culture")
            mentions = []
            for t in turns:
                q = t.get("doctor_output", {}).get("chosen_question", "")
                if q and any(kw in q.lower() for kw in _test_keywords):
                    mentions.append(q)
            if mentions:
                doctor_test_mentions = "Tests/investigations mentioned by the doctor during the session:\n" + \
                    "\n".join(f'- "{m}"' for m in mentions)
    except Exception:
        pass

    prompt = f"""You are a clinical decision-support system.

{patient_line}
Chief complaint: {chief_complaint or "not specified"}
Symptoms: {symptoms_text or "not specified"}
Top differential: {diff_summary}
Most likely diagnosis: {top_dx}
{doctor_test_mentions}

Your task: Decide which BASIC outpatient tests are warranted BEFORE a specialist appointment.
Rules:
- If the doctor mentioned a specific test during the session (e.g. chest X-ray, ECG, blood test), you MUST include it.
- Only suggest tests that are simple, widely available, and clearly indicated.
- Maximum 5 tests. If no tests are needed, say so.
- Do NOT suggest invasive procedures, biopsies, or specialist-only tests.
- Allowed categories: blood, urine, imaging (plain X-ray, ECG, basic ultrasound), vitals.

Respond with ONLY valid JSON, no markdown, no explanation outside the JSON:
{{
  "necessary": true,
  "rationale": "one sentence explaining why tests are or aren't needed",
  "tests": [
    {{
      "id": "t1",
      "name": "Complete Blood Count (CBC)",
      "category": "blood",
      "priority": "routine",
      "why": "one sentence why this test helps",
      "preparation": "No special preparation required.",
      "duration": "15 minutes",
      "where": "Any diagnostic lab or GP clinic"
    }}
  ]
}}

Priority must be one of: routine, urgent.
Category must be one of: blood, urine, imaging, vitals.
If no tests needed: set necessary=false and tests=[].
"""

    try:
        from loop1.llm import call_llm
        model = os.environ.get("CRITIC_MODEL", "openrouter/google/gemma-4-31b-it:free")
        raw = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                functools.partial(call_llm, model, [{"role": "user", "content": prompt}], temperature=0, max_tokens=1024)
            ),
            timeout=35.0,
        )
        # Strip markdown fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("```").strip()
        result = json.loads(raw)
        # Ensure required fields
        result.setdefault("necessary", bool(result.get("tests")))
        result.setdefault("rationale", "")
        result.setdefault("tests", [])
        # Assign stable IDs
        for i, t in enumerate(result["tests"]):
            t.setdefault("id", f"t{i+1}")
            t.setdefault("priority", "routine")
            t.setdefault("preparation", "No special preparation required.")
            t.setdefault("duration", "")
            t.setdefault("where", "Any diagnostic lab or GP clinic")
    except asyncio.TimeoutError:
        _log.warning("suggested-tests LLM call timed out for session %s", session_id)
        raise HTTPException(status_code=504, detail="Test analysis timed out. Please retry.")
    except Exception as exc:
        _log.warning("suggested-tests LLM call failed: %s", exc)
        result = {"necessary": False, "rationale": "Test suggestions unavailable.", "tests": []}

    _suggested_tests_cache[session_id] = result
    return result


@router.post("/api/session/{session_id}/book")
async def book_appointment(session_id: str, req: BookRequest, request: Request):
    """Book a slot with a doctor for a completed session."""
    from web.api import (
        _add_session_to_user,
        _get_final_differential,
        _get_user_from_request,
        _load_appointments,
        _load_doctors,
        _load_file_data,
        _load_report_from_disk,
        _load_session_report_from_db,
        _load_session_uploads,
        _load_users,
        _save_appointments,
        _save_doctors,
        _save_file_data,
        _save_session_uploads,
        _session_test_uploads,
        _sessions,
    )
    from loop3.routing.router import route as compute_routing

    user = _get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    # Try to get routing from live session or disk
    diff, confidence = _get_final_differential(session_id)

    urgency = "routine"
    if diff:
        routing = compute_routing(session_id, diff, confidence)
        urgency = routing.primary_routing.urgency.value
        if urgency == "emergency":
            raise HTTPException(status_code=400, detail="Emergency cases cannot be booked — go to the nearest ED.")
    else:
        # Session not in memory/disk (e.g. after restart) — verify via user's persisted session list
        user_session = next(
            (s for s in user.get("sessions", []) if s.get("session_id") == session_id),
            None,
        )
        if user_session is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        # We can't re-derive urgency, default to routine (emergency check already ran client-side)

    # Validate doctor + slot
    all_doctors = _load_doctors()
    doctor = next((d for d in all_doctors if d["id"] == req.doctor_id), None)
    if doctor is None:
        raise HTTPException(status_code=404, detail="Doctor not found.")
    if req.slot not in doctor["available_slots"]:
        raise HTTPException(status_code=400, detail="Slot not available.")

    # Resolve primary diagnosis + demographics: disk → in-memory → user session list
    primary_diagnosis = ""
    pat_age, pat_sex, pat_bmi = None, None, None
    disk_rec = _load_report_from_disk(session_id)
    if disk_rec:
        primary_diagnosis = disk_rec.get("final_diagnosis", "")
        pat_age = disk_rec.get("patient", {}).get("age")
        pat_sex = disk_rec.get("patient", {}).get("sex")
    else:
        live = _sessions.get(session_id)
        if live and live._final_record:
            primary_diagnosis = live._final_record.primary_diagnosis or ""
            demo = live._final_record.final_profile.demographics if live._final_record.final_profile else None
            if demo:
                pat_age = demo.age
                pat_sex = demo.sex
                pat_bmi = demo.other.get("bmi") if demo.other else None
        else:
            user_session = next(
                (s for s in user.get("sessions", []) if s.get("session_id") == session_id),
                None,
            )
            if user_session:
                primary_diagnosis = user_session.get("primary_diagnosis", "")

    # Fall back to user profile for demographics
    if pat_age is None:
        pat_age = user.get("age")
    if pat_sex is None:
        pat_sex = user.get("sex")
    if pat_bmi is None:
        pat_bmi = user.get("bmi")

    appt_id = str(uuid.uuid4())
    appt = {
        "appointment_id": appt_id,
        "session_id": session_id,
        "patient_user_id": user["id"],
        "patient_name": user.get("name", "Anonymous Patient"),
        "doctor_id": req.doctor_id,
        "doctor_name": doctor["name"],
        "specialty": doctor["specialty"],
        "slot": req.slot,
        "status": "upcoming",
        "booked_at": datetime.now(timezone.utc).isoformat(),
        "primary_diagnosis": primary_diagnosis,
        "urgency": urgency,
        "age": pat_age,
        "sex": pat_sex,
        "bmi": pat_bmi,
    }
    # Attach any session uploads that were pending before the appointment existed.
    # Merge in-memory cache with disk (disk is authoritative across restarts).
    disk_uploads = _load_session_uploads()
    disk_bucket = disk_uploads.get(session_id, {})
    mem_bucket = _session_test_uploads.get(session_id, {})
    pending_uploads = {**disk_bucket, **mem_bucket}  # mem wins on key conflict
    if pending_uploads:
        # Migrate file data from session key to appointment key; strip data_b64 inline
        for tid, rec in pending_uploads.items():
            fname = rec.get("filename", "")
            if fname:
                file_data = (
                    _load_file_data(f"session:{session_id}", fname)
                    or rec.get("data_b64", "")
                )
                if file_data:
                    _save_file_data(appt_id, fname, file_data)
        appt["patient_files"] = [
            {k: v for k, v in rec.items() if k != "data_b64"}
            for rec in pending_uploads.values()
        ]
        appt["suggested_test_uploads"] = {
            tid: {"filename": rec["filename"], "uploaded_at": rec["uploaded_at"],
                  "test_name": rec.get("suggested_test_name") or tid}
            for tid, rec in pending_uploads.items()
            if rec.get("suggested_test_id")
        }

    appointments = _load_appointments()
    appointments[appt_id] = appt
    _save_appointments(appointments)

    # Clean up persisted pre-booking uploads now that they're in the appointment
    if session_id in _session_test_uploads:
        del _session_test_uploads[session_id]
    disk_uploads.pop(session_id, None)
    _save_session_uploads(disk_uploads)

    # Remove the booked slot from the doctor's available slots
    doctor["available_slots"] = [s for s in doctor["available_slots"] if s != req.slot]
    _save_doctors(all_doctors)

    # Ensure the session appears in the user's session history.
    # If the session was started anonymously (not logged in), it won't be in the
    # user's list yet even though the appointment links to it. Add it now.
    if session_id and user:
        users = _load_users()
        uid = user["id"]
        existing_ids = {s.get("session_id") for s in users[uid].get("sessions", [])}
        if session_id not in existing_ids:
            report = _load_session_report_from_db(session_id)
            chief_complaint = ""
            started_at = appt.get("booked_at", datetime.now(timezone.utc).isoformat())
            final_dx = None
            ended_at = None
            status = "active"
            if report:
                chief_complaint = report.get("patient", {}).get("chief_complaint", "")
                started_at = report.get("started_at", started_at)
                final_dx = report.get("primary_diagnosis") or report.get("final_differential", [{}])[0].get("dx") if report.get("final_differential") else None
                ended_at = report.get("ended_at")
                status = "complete" if report.get("termination_reason") else "active"
            _add_session_to_user(uid, {
                "session_id": session_id,
                "chief_complaint": chief_complaint,
                "started_at": started_at,
                "status": status,
                "primary_diagnosis": final_dx,
                "ended_at": ended_at,
            })

    return {"appointment_id": appt_id, "confirmed": True, "doctor_name": doctor["name"], "slot": req.slot}
