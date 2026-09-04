"""/api/session/*, /api/cases/* — moved from web/api.py, Task 4's third
split-out router (part 1 of 2 — see routers/session_booking.py for
suggested-tests and book, split into a second file to stay under the
400-line limit). Logic unchanged from the original; only the location
moved and manual `_get_user_from_request` + 401 checks became
Depends(get_current_user) where the original required auth (most of these
routes don't — sessions can be started anonymously).

Helpers verified private to this domain (not referenced anywhere else in
web/api.py) moved fully: _CASE_META, _load_profile, _profile_summary,
_suggested_tests_cache (the last of these is used by session_booking.py
too — imported from here rather than duplicated). _get_final_differential
did NOT move — it's also used by a route that hasn't been split out yet
(web/api.py ~L2060 as of this writing) — so it stays behind and gets
lazily imported, same as the other genuinely-shared helpers. See
TASK4_SPLIT_ROUTERS.md §3.

Also bundled in here: POST /api/tts (the Sarvam translate+TTS one). It
sits contiguously inside this domain's block in the original file, so it
moved with the cut rather than getting its own extraction pass. Important:
there are TWO `POST /api/tts` routes in the original app — this one
(registered first, wins) and a second, completely different ElevenLabs
proxy implementation far later in web/api.py (~L3414) that is permanently
shadowed and unreachable. That's a pre-existing bug, not introduced by
this split, and out of scope to fix here (Task 4 is zero-behavior-change).
Preserved by registering this router early via app.include_router(),
same as every other router — same precedence as before the split.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from diffdx.db.engine import get_sessionmaker
from diffdx.repositories.sessions import DiagnosticSessionRepository
from diffdx.schemas.sessions import StartCustomRequest, StartRequest, TurnRequest
from diffdx.legacy_store import (
    _CASES_DIR,
    _add_session_to_user,
    _get_final_differential,
    _get_user_from_request,
    _load_blocked_dates,
    _load_doctors,
    _load_report_from_disk,
    _log,
    _sarvam_translate,
    _sarvam_tts_b64,
    _save_session_report,
    _update_session_in_user,
)
from diffdx.session_store import get_session, save_session
from web.api_session import APISession

router = APIRouter(tags=["sessions"])

_CASE_META = {
    "case_01": {"label": "Case 01", "tags": ["cardiac", "respiratory"]},
    "case_02": {"label": "Case 02", "tags": ["neurology"]},
    "case_03": {"label": "Case 03", "tags": ["gastroenterology"]},
    "case_04": {"label": "Case 04", "tags": ["pediatric", "infectious"]},
    "case_05": {"label": "Case 05", "tags": ["geriatric", "vague"]},
    "case_06": {"label": "Case 06", "tags": ["pediatric", "infectious"]},
}

_suggested_tests_cache: dict[str, dict] = {}


def _load_profile(case_id: str):
    from loop1.schemas import PatientProfile

    path = _CASES_DIR / f"{case_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Case '{case_id}' not found.")
    data = json.loads(path.read_text())
    return PatientProfile.model_validate(data)


def _profile_summary(profile) -> dict:
    return {
        "chief_complaint": profile.chief_complaint,
        "demographics": profile.demographics.model_dump(),
        "symptoms": [s.model_dump() for s in profile.symptoms],
        "history": profile.history.model_dump(),
        "free_notes": profile.free_notes,
    }


def _persist_completed_session(session: APISession, patient_id: uuid.UUID | None) -> None:
    """Write-through to Postgres once a session completes — week1.md Task 6:
    "Completed sessions persist to Postgres (DiagnosticSession +
    SessionTurn); Redis holds only in-flight state." Best-effort, same
    pattern as every other dual-write in this codebase: a failure here must
    never break the turn/init response that triggered it. The full report
    (including critiques, which have no column here) stays sourced from the
    existing blob/disk path — this is a write-through for durability, not a
    read-path flip."""
    final = session._final_record
    if final is None:
        return
    try:
        session_uuid = uuid.UUID(session.session_id)
        with get_sessionmaker()() as db:
            repo = DiagnosticSessionRepository(db)
            repo.upsert(
                session_uuid,
                patient_id=patient_id,
                chief_complaint=session.profile.chief_complaint,
                primary_diagnosis=final.primary_diagnosis,
                termination_reason=final.termination_reason,
                started_at=datetime.fromisoformat(final.started_at),
                ended_at=datetime.fromisoformat(final.ended_at),
                total_turns=len(session.history),
                final_differential=[{"dx": d.dx, "prob": d.prob} for d in final.final_differential],
                closing_turn=final.closing_turn.model_dump(mode="json") if final.closing_turn else None,
            )
            for tr in session.history:
                repo.add_turn(
                    session_uuid, tr.turn_index,
                    question=tr.doctor_output.chosen_question,
                    rationale=tr.doctor_output.rationale,
                    biggest_uncertainty=tr.doctor_output.biggest_uncertainty,
                    patient_answer=tr.patient_answer,
                    confidence=tr.doctor_output.confidence_to_stop,
                    differential=[
                        {"dx": d.dx, "prob": d.prob} for d in tr.doctor_output.current_differential
                    ],
                    doctor_output=tr.doctor_output.model_dump(mode="json"),
                )
            db.commit()
    except Exception:
        _log.warning("Postgres persistence failed for session %s", session.session_id, exc_info=True)


@router.get("/api/cases")
async def list_cases():
    """Return metadata for all available test cases."""

    cases = []
    for case_id in sorted(_CASES_DIR.glob("case_0*.json"), key=lambda p: p.name):
        cid = case_id.stem
        try:
            profile = _load_profile(cid)
        except Exception:
            continue
        meta = _CASE_META.get(cid, {})
        cases.append({
            "case_id": cid,
            "label": meta.get("label", cid.replace("_", " ").title()),
            "tags": meta.get("tags", []),
            "chief_complaint": profile.chief_complaint,
            "demographics": profile.demographics.model_dump(),
        })
    return {"cases": cases}


@router.get("/api/cases/{case_id}")
async def get_case(case_id: str):
    """Return the full profile for a single case."""
    profile = _load_profile(case_id)
    return {"case_id": case_id, **_profile_summary(profile)}


@router.post("/api/tts")
async def tts_proxy(request: Request):
    """Translate English text to target language, then synthesise via Sarvam TTS."""

    body = await request.json()
    text = body.get("text", "")
    lang = body.get("lang", "hi-IN")
    # Translate English → target language first
    translated = _sarvam_translate(text, "en-IN", lang) if lang != "en-IN" else text
    audio_b64 = _sarvam_tts_b64(translated, lang)
    if not audio_b64:
        raise HTTPException(status_code=503, detail="TTS unavailable")
    return {"audio_b64": audio_b64, "translated_text": translated}


@router.post("/api/session/start")
def start_session(req: StartRequest, request: Request):
    """
    Start a new session for a given case.
    Returns session_id, patient demographics, and the first doctor question.
    """

    profile = _load_profile(req.case_id)
    profile.session_id = str(uuid.uuid4())

    session = APISession(profile, max_turns=6)
    session.session_language = req.session_language
    session._on_change = lambda: save_session(session.session_id, session)
    try:
        turn_result = session.initialize()
    except Exception as exc:
        _log.error("Session init failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    save_session(session.session_id, session)
    if session.complete:
        user = _get_user_from_request(request)
        _persist_completed_session(session, uuid.UUID(user["id"]) if user else None)

    return {
        "session_id": session.session_id,
        "case_id": req.case_id,
        "session_language": req.session_language,
        **_profile_summary(profile),
        **turn_result,
    }


@router.post("/api/session/start-custom")
def start_custom_session(req: StartCustomRequest, request: Request):
    """
    Start a session from a patient-provided free-form profile.
    No pre-loaded case needed — works with any patient data.
    """
    from loop1.schemas import Demographics, History, PatientProfile, Symptom

    session_id = str(uuid.uuid4())
    other: dict = {}
    if req.occupation:
        other["occupation"] = req.occupation
    if req.bmi is not None:
        other["bmi"] = req.bmi

    profile = PatientProfile(
        session_id=session_id,
        demographics=Demographics(age=req.age, sex=req.sex, other=other),
        chief_complaint=req.chief_complaint,
        symptoms=[
            Symptom(name=s.name, onset=s.onset, severity=s.severity, notes=s.notes)
            for s in req.symptoms
        ],
        history=History(
            medical=req.history.medical,
            medications=req.history.medications,
            allergies=req.history.allergies,
            family=req.history.family,
            social=req.history.social,
        ),
        free_notes=req.free_notes,
    )

    session = APISession(profile, max_turns=6)
    session.session_language = req.session_language
    session._on_change = lambda: save_session(session.session_id, session)
    try:
        turn_result = session.initialize()
    except Exception as exc:
        _log.error("Custom session init failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=503, detail=str(exc))

    save_session(session.session_id, session)

    user = _get_user_from_request(request)
    if user:
        _add_session_to_user(user["id"], {
            "session_id": session.session_id,
            "chief_complaint": req.chief_complaint,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "active",
            "primary_diagnosis": None,
            "ended_at": None,
        })
    if session.complete:
        _persist_completed_session(session, uuid.UUID(user["id"]) if user else None)

    return {
        "session_id": session.session_id,
        "session_language": req.session_language,
        **_profile_summary(profile),
        **turn_result,
    }


@router.post("/api/session/{session_id}/turn")
def submit_turn(session_id: str, req: TurnRequest, request: Request):
    """
    Submit the patient's answer to the current doctor question.
    Returns the next question, updated differential, and real-time critique.
    """

    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session.complete:
        raise HTTPException(status_code=400, detail="Session already complete.")
    session._on_change = lambda: save_session(session_id, session)

    lang = getattr(session, "session_language", "en-IN")
    patient_answer = req.patient_answer
    if lang and lang != "en-IN":
        # Translate patient answer to English so LLM always sees English
        patient_answer = _sarvam_translate(patient_answer, lang, "en-IN")

    try:
        result = session.submit_answer(patient_answer)
    except Exception as exc:
        _log.error("Turn failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    # submit_answer() mutates `session` in place — with an in-memory dict
    # that "just worked" via the shared reference; Redis needs an explicit
    # write-back (see APISession._on_change for the same hazard on the
    # async critic path).
    save_session(session_id, session)
    if session.complete:
        user = _get_user_from_request(request)
        _persist_completed_session(session, uuid.UUID(user["id"]) if user else None)

    # Return English question — frontend fetches translation+TTS in parallel
    return result


@router.get("/api/session/{session_id}/report")
def get_report(session_id: str, request: Request):
    """Return the full critic report once the session is complete."""

    session = get_session(session_id)
    if session is None:
        # Fall back to on-disk final record (survives server restarts)
        report = _load_report_from_disk(session_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        user = _get_user_from_request(request)
        if user:
            _update_session_in_user(user["id"], session_id, report.get("final_diagnosis", ""), report.get("ended_at", ""))
        return report
    if not session.complete:
        raise HTTPException(status_code=400, detail="Session not yet complete.")
    report = session.get_report()
    if report is None:
        raise HTTPException(status_code=500, detail="Report generation failed.")
    # Persist to DB so report survives server restarts / deploys
    _save_session_report(session_id, report)
    user = _get_user_from_request(request)
    if user:
        _update_session_in_user(
            user["id"], session_id,
            report.get("final_diagnosis", ""),
            datetime.now(timezone.utc).isoformat(),
        )
    return report


@router.get("/api/session/{session_id}/routing")
def get_routing(session_id: str):
    """Return the RoutingDecision for a completed session plus matching doctors."""
    from loop3.routing.router import route as compute_routing

    try:
        diff, confidence = _get_final_differential(session_id)
    except Exception as exc:
        _log.error("get_routing: _get_final_differential failed for %s: %s", session_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to load session differential: {exc}")
    if diff is None:
        raise HTTPException(status_code=404, detail="Session not found or not complete.")
    if not diff:
        raise HTTPException(status_code=400, detail="Session has no differential to route.")

    try:
        routing = compute_routing(session_id, diff, confidence)
    except Exception as exc:
        _log.error("get_routing: compute_routing failed for %s: %s", session_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Routing engine error: {exc}")
    primary = routing.primary_routing
    is_emergency = primary.urgency.value == "emergency"

    # Load matching doctors (skipped for emergency — hospitals shown instead)
    doctors = []
    if not is_emergency:
        all_doctors = _load_doctors()
        blocked_data = _load_blocked_dates()
        now_prefix = datetime.now(timezone.utc).isoformat()[:16]

        def _future_slots_routing(doc_id, slots):
            blocked = {d["date"] for d in blocked_data.get(doc_id, [])}
            return sorted(s for s in slots if s >= now_prefix and s[:10] not in blocked)

        doctors = [
            {
                "id": d["id"],
                "name": d["name"],
                "hospital": d["hospital"],
                "rating": d["rating"],
                "avatar_initials": d["avatar_initials"],
                "available_slots": _future_slots_routing(d.get("id", ""), d.get("available_slots", [])),
            }
            for d in all_doctors
            if d["specialty"] == primary.specialty
        ]

    return {
        "specialty": primary.specialty,
        "urgency": primary.urgency.value,
        "appointment_type": primary.appointment_type.value,
        "reasoning": primary.reasoning,
        "is_emergency": is_emergency,
        "is_ambiguous": routing.is_ambiguous,
        "final_confidence": routing.final_confidence,
        "doctors": doctors,
    }

