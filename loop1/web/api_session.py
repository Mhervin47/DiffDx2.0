"""
APISession: step-based diagnostic session for the web API.

Unlike Session.run() which loops interactively, APISession exposes
two methods: initialize() and submit_answer(). Each returns a JSON-
serialisable dict with the doctor question, differential, and critique.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# Load .env before importing loop1 modules
try:
    import dotenv
    dotenv.load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

os.environ.setdefault("CRITIC_MODEL", "openrouter/meta-llama/llama-3.3-70b-instruct")

from loop1.closing_turn import generate_closing_turn_with_usage
from loop1.compressor import compress_context
from loop1.config import config
from loop1.doctor import generate_turn_with_usage
from loop1.llm import LlmUsage
from loop1.logging_utils import log_event, write_final_record
from loop1.safety import check_safety
from loop1.schemas import (
    DiagnosisEntry,
    DoctorTurnOutput,
    FinalRecord,
    ModelMetadata,
    PatientProfile,
    TurnRecord,
)
from loop2.critic.aggregator import aggregate_critiques
from loop2.critic.critic import critique_turn
from loop2.critic.critique_schema import TurnCritique

try:
    from admin_portal.instrumentation import usage_logger as _usage_logger
except Exception:          # admin_portal is optional; the session path must run without it
    _usage_logger = None

_log = logging.getLogger(__name__)


class APISession:
    """
    Manages one diagnostic session driven turn-by-turn from the web API.

    Flow:
        session = APISession(profile)
        result = session.initialize()          # returns first doctor question
        result = session.submit_answer(text)   # returns next question + critique
        ...until result["session_complete"] is True
        report = session.get_report()
    """

    def __init__(self, profile: PatientProfile, max_turns: int = 6) -> None:
        self.profile = profile
        self.session_id = profile.session_id
        self.max_turns = max_turns
        self.confidence_threshold: float = config["thresholds"]["confidence_to_stop"]
        self.keep_recent: int = config["thresholds"]["compression_keep_recent"]

        self.history: list[TurnRecord] = []
        self._live_events: list[dict] = []
        self._critiques: list[TurnCritique] = []
        self.started_at = datetime.now(timezone.utc).isoformat()

        # Pending question state
        self._pending_turn_index: int = 0
        self._pending_doctor_output: DoctorTurnOutput | None = None
        self._pending_usage: LlmUsage | None = None
        self._pending_exemplar_ids: list[str] = []

        self.complete: bool = False
        self.termination_reason: str | None = None
        self._final_record: FinalRecord | None = None

        # Set by the router right after start_session/start_custom_session
        # construct this (see routers/sessions.py) — declared here so it's
        # captured by to_dict()/from_dict() instead of silently dropped.
        self.session_language: str = "en-IN"

        # Optional callback the router attaches after every get_session()/
        # construction (see src/diffdx/session_store.py). NOT serialized —
        # from_dict() always leaves this None. _fire_critic's background
        # thread calls it (if set) after appending to self._critiques, so
        # that mutation reaches the session store even though it happens
        # after the HTTP response for the turn that triggered it has
        # already gone out — a live in-memory dict "just works" here via
        # the shared reference, but Redis needs an explicit write-back.
        self._on_change: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initialize(self) -> dict:
        """Generate the first doctor question. Call once after creation."""
        log_event(
            session_id=self.session_id,
            event_type="session_start",
            data={
                "session_id": self.session_id,
                "chief_complaint": self.profile.chief_complaint,
                "max_turns": self.max_turns,
                "confidence_threshold": self.confidence_threshold,
            },
        )
        doctor_output, usage, exemplar_ids = self._generate_turn_with_usage_logged(
            0, "initialize"
        )

        if doctor_output.should_stop or doctor_output.confidence_to_stop >= self.confidence_threshold:
            self._finalize("confidence_threshold", doctor_output)
            return self._build_response(doctor_output, None)

        self._set_pending(0, doctor_output, usage, exemplar_ids)
        return self._build_response(doctor_output, None)

    def submit_answer(self, patient_answer: str) -> dict:
        """Log the patient answer, run critique, and generate the next question."""
        if self.complete:
            raise ValueError("Session is already complete.")
        if self._pending_doctor_output is None:
            raise ValueError("No pending question — call initialize() first.")

        doctor_output = self._pending_doctor_output
        turn_index = self._pending_turn_index

        # Safety check
        matched = check_safety(patient_answer)
        if matched:
            self._finalize("safety_stop", doctor_output)
            return self._build_response(doctor_output, None)

        # Build event dict (mirrors LiveCriticSession._log_turn)
        now = datetime.now(timezone.utc).isoformat()
        event = {
            "event_type": "turn_complete",
            "session_id": self.session_id,
            "turn_index": turn_index,
            "doctor_output": doctor_output.model_dump(),
            "patient_answer": patient_answer,
            "profile_state": self.profile.model_dump(),
            "prompt_tokens": self._pending_usage.prompt_tokens if self._pending_usage else None,
            "timestamp": now,
        }
        self._live_events.append(event)

        turn_record = TurnRecord(
            turn_index=turn_index,
            doctor_output=doctor_output,
            patient_answer=patient_answer,
            retrieved_exemplar_ids=self._pending_exemplar_ids,
            timestamp=now,
        )
        log_event(
            session_id=self.session_id,
            event_type="turn_complete",
            data={
                "turn_index": turn_index,
                "doctor_output": doctor_output.model_dump(),
                "patient_answer": patient_answer,
                "retrieved_exemplar_ids": self._pending_exemplar_ids,
                "prompt_tokens": self._pending_usage.prompt_tokens if self._pending_usage else None,
                "timestamp": now,
                "profile_state": self.profile.model_dump(),
            },
        )
        self.history.append(turn_record)

        # Fire critic in background — never wait for it. Scores appear in the final report.
        # OpenRouter/Gemma-4 free tier runs on a separate quota from Groq/Gemini.
        self._fire_critic(event, turn_index)

        # Profile updater disabled in web session — it makes an extra Groq call per turn
        # that consistently rate-limits subsequent doctor question calls on Groq's free tier.
        # The full turn history passed to the doctor already provides enough context.
        self._maybe_compress()

        next_index = turn_index + 1

        # Max turns reached — one more generation for the final differential, then close
        if next_index >= self.max_turns:
            final_output, _usage, _ = self._generate_turn_with_usage_logged(
                next_index, "final_generation"
            )
            self._finalize("max_turns", final_output)
            return self._build_response(final_output, None)

        # Generate next question
        next_output, next_usage, next_exemplar_ids = self._generate_turn_with_usage_logged(
            next_index, "next_question"
        )

        if next_output.should_stop or next_output.confidence_to_stop >= self.confidence_threshold:
            self._finalize("confidence_threshold", next_output)
            return self._build_response(next_output, None)

        self._set_pending(next_index, next_output, next_usage, next_exemplar_ids)
        return self._build_response(next_output, None)

    def get_report(self) -> dict | None:
        """Return the full critic report JSON once the session is complete."""
        if not self._final_record:
            return None
        agg = aggregate_critiques(self._critiques) if self._critiques else {}
        closing = self._final_record.closing_turn
        return {
            "schema_version": "0.7.0",
            "session_id": self.session_id,
            "patient": {
                "age": self.profile.demographics.age,
                "sex": self.profile.demographics.sex,
                "chief_complaint": self.profile.chief_complaint,
            },
            "final_diagnosis": self._final_record.primary_diagnosis,
            "termination_reason": self._final_record.termination_reason,
            "total_turns": len(self._critiques),
            "aggregate": agg,
            "turns": [c.model_dump() for c in self._critiques],
            "closing_turn": closing.model_dump() if closing else None,
            "final_differential": [
                {"dx": d.dx, "prob": d.prob}
                for d in self._final_record.final_differential
            ],
            "turn_history": [
                {
                    "turn_index": tr.turn_index,
                    "question": tr.doctor_output.chosen_question,
                    "rationale": tr.doctor_output.rationale,
                    "patient_answer": tr.patient_answer,
                    "differential": [
                        {"dx": d.dx, "prob": d.prob}
                        for d in tr.doctor_output.current_differential
                    ],
                    "confidence": tr.doctor_output.confidence_to_stop,
                }
                for tr in self._final_record.turn_history
            ],
        }

    def to_dict(self) -> dict:
        """JSON-safe snapshot of every field a fresh session needs to
        resume from — used by src/diffdx/session_store.py to persist to
        Redis (or restore from the in-memory fallback in a uniform way).
        Every field here is either a primitive or a pydantic BaseModel;
        `_on_change` (a live callback, not data) is deliberately excluded —
        the caller re-attaches it after from_dict()."""
        return {
            "profile": self.profile.model_dump(mode="json"),
            "session_id": self.session_id,
            "max_turns": self.max_turns,
            "confidence_threshold": self.confidence_threshold,
            "keep_recent": self.keep_recent,
            "history": [t.model_dump(mode="json") for t in self.history],
            "_live_events": self._live_events,
            "_critiques": [c.model_dump(mode="json") for c in self._critiques],
            "started_at": self.started_at,
            "_pending_turn_index": self._pending_turn_index,
            "_pending_doctor_output": (
                self._pending_doctor_output.model_dump(mode="json")
                if self._pending_doctor_output is not None else None
            ),
            "_pending_usage": (
                asdict(self._pending_usage) if self._pending_usage is not None else None
            ),
            "_pending_exemplar_ids": self._pending_exemplar_ids,
            "complete": self.complete,
            "termination_reason": self.termination_reason,
            "_final_record": (
                self._final_record.model_dump(mode="json")
                if self._final_record is not None else None
            ),
            "session_language": self.session_language,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "APISession":
        """Reconstruct from to_dict()'s output via __new__, not __init__ —
        __init__ takes a fresh PatientProfile and re-derives
        confidence_threshold/keep_recent from global config, which would
        silently diverge from what was actually serialized if config
        changed between processes. Every field is restored directly
        instead."""
        obj = cls.__new__(cls)
        obj.profile = PatientProfile.model_validate(data["profile"])
        obj.session_id = data["session_id"]
        obj.max_turns = data["max_turns"]
        obj.confidence_threshold = data["confidence_threshold"]
        obj.keep_recent = data["keep_recent"]
        obj.history = [TurnRecord.model_validate(t) for t in data["history"]]
        obj._live_events = data["_live_events"]
        obj._critiques = [TurnCritique.model_validate(c) for c in data["_critiques"]]
        obj.started_at = data["started_at"]
        obj._pending_turn_index = data["_pending_turn_index"]
        obj._pending_doctor_output = (
            DoctorTurnOutput.model_validate(data["_pending_doctor_output"])
            if data["_pending_doctor_output"] is not None else None
        )
        obj._pending_usage = (
            LlmUsage(**data["_pending_usage"]) if data.get("_pending_usage") is not None else None
        )
        obj._pending_exemplar_ids = data["_pending_exemplar_ids"]
        obj.complete = data["complete"]
        obj.termination_reason = data["termination_reason"]
        obj._final_record = (
            FinalRecord.model_validate(data["_final_record"])
            if data["_final_record"] is not None else None
        )
        obj.session_language = data.get("session_language", "en-IN")
        obj._on_change = None
        return obj

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _recent_history(self) -> list[TurnRecord]:
        if self.profile.running_summary:
            return self.history[-self.keep_recent:]
        return self.history

    def _generate_turn_with_usage_logged(self, turn_index: int, call_site: str):
        """Wraps generate_turn_with_usage with latency timing and a usage-log
        call (admin_portal, optional). Returns its result completely
        unchanged; never swallows an exception — only adds logging."""
        result = None
        ok = True
        error_type = None
        t0 = time.perf_counter()
        try:
            result = generate_turn_with_usage(self.profile, self._recent_history(), turn_index)
            return result
        except Exception as exc:
            ok = False
            error_type = type(exc).__name__
            raise
        finally:
            if _usage_logger is not None:
                latency_ms = (time.perf_counter() - t0) * 1000
                doctor_output, usage = (result[0], result[1]) if result is not None else (None, None)
                _usage_logger.log_turn_usage(
                    session_id=self.session_id,
                    turn_index=turn_index,
                    call_site=call_site,
                    prompt_tokens=usage.prompt_tokens if usage else None,
                    completion_tokens=usage.completion_tokens if usage else None,
                    total_tokens=usage.total_tokens if usage else None,
                    model_actual=usage.model_actual if usage else None,
                    doctor_output=doctor_output,
                    latency_ms=latency_ms,
                    ok=ok,
                    error_type=error_type,
                )

    def _maybe_compress(self) -> None:
        if len(self.history) <= self.keep_recent:
            return
        t0 = time.perf_counter()
        ok, error_type, usage = True, None, None
        try:
            self.profile, usage = compress_context(self.profile, self.history, self.keep_recent)
        except Exception as exc:
            ok = False
            error_type = type(exc).__name__
            raise
        finally:
            if _usage_logger is not None:
                latency_ms = (time.perf_counter() - t0) * 1000
                _usage_logger.log_turn_usage(
                    session_id=self.session_id,
                    turn_index=self._pending_turn_index,
                    call_site="compressor",
                    prompt_tokens=usage.prompt_tokens if usage else None,
                    completion_tokens=usage.completion_tokens if usage else None,
                    total_tokens=usage.total_tokens if usage else None,
                    model_actual=usage.model_actual if usage else None,
                    doctor_output=None,
                    latency_ms=latency_ms,
                    ok=ok,
                    error_type=error_type,
                )
        log_event(
            session_id=self.session_id,
            event_type="compression_complete",
            data={
                "turns_summarized": len(self.history) - self.keep_recent,
                "running_summary_length": len(self.profile.running_summary),
            },
        )

    def _set_pending(
        self,
        turn_index: int,
        doctor_output: DoctorTurnOutput,
        usage: LlmUsage,
        exemplar_ids: list[str],
    ) -> None:
        self._pending_turn_index = turn_index
        self._pending_doctor_output = doctor_output
        self._pending_usage = usage
        self._pending_exemplar_ids = exemplar_ids

    def _finalize(self, termination_reason: str, final_doctor_output: DoctorTurnOutput) -> None:
        self.complete = True
        self.termination_reason = termination_reason

        closing = None
        if termination_reason != "safety_stop":
            t0 = time.perf_counter()
            ok, error_type, usage = True, None, None
            try:
                closing, usage = generate_closing_turn_with_usage(
                    self.profile, final_doctor_output.current_differential
                )
                if closing is None:
                    ok = False
                    error_type = "ClosingTurnGenerationFailed"
            except Exception as exc:
                ok = False
                error_type = type(exc).__name__
                raise
            finally:
                if _usage_logger is not None:
                    latency_ms = (time.perf_counter() - t0) * 1000
                    _usage_logger.log_turn_usage(
                        session_id=self.session_id,
                        turn_index=len(self.history),
                        call_site="closing_turn",
                        prompt_tokens=usage.prompt_tokens if usage else None,
                        completion_tokens=usage.completion_tokens if usage else None,
                        total_tokens=usage.total_tokens if usage else None,
                        model_actual=usage.model_actual if usage else None,
                        doctor_output=None,
                        latency_ms=latency_ms,
                        ok=ok,
                        error_type=error_type,
                    )

        primary = (
            final_doctor_output.current_differential[0].dx
            if final_doctor_output.current_differential
            else "undetermined"
        )
        self._final_record = FinalRecord(
            session_id=self.session_id,
            started_at=self.started_at,
            ended_at=datetime.now(timezone.utc).isoformat(),
            termination_reason=termination_reason,  # type: ignore[arg-type]
            final_profile=self.profile,
            final_differential=final_doctor_output.current_differential,
            primary_diagnosis=primary,
            turn_history=self.history,
            model_metadata=ModelMetadata(
                model_name=config["models"]["doctor"],
                model_version="0",
                prompt_template_version=config["prompt_versions"]["doctor"],
            ),
            closing_turn=closing,
        )
        write_final_record(self.session_id, self._final_record.model_dump())
        log_event(
            session_id=self.session_id,
            event_type="session_end",
            data={
                "termination_reason": termination_reason,
                "doctor_output": final_doctor_output.model_dump(),
            },
        )

    def _fire_critic(self, event: dict, turn_index: int) -> None:
        """Submit a critique task and return immediately. Result stored in self._critiques."""
        def _run() -> None:
            import time
            time.sleep(turn_index * 2)  # stagger calls so they don't all hit rate limits together
            t0 = time.perf_counter()
            ok, error_type, usage = True, None, None
            try:
                result, usage = critique_turn(event, self._live_events, self.session_id)
                if result:
                    self._critiques.append(result)
                    if self._on_change is not None:
                        self._on_change()
            except Exception as exc:
                ok = False
                error_type = type(exc).__name__
                _log.warning("Critic failed for turn %d: %s", turn_index, exc)
            finally:
                if _usage_logger is not None:
                    latency_ms = (time.perf_counter() - t0) * 1000
                    _usage_logger.log_turn_usage(
                        session_id=self.session_id,
                        turn_index=turn_index,
                        call_site="critic",
                        prompt_tokens=usage.prompt_tokens if usage else None,
                        completion_tokens=usage.completion_tokens if usage else None,
                        total_tokens=usage.total_tokens if usage else None,
                        model_actual=usage.model_actual if usage else None,
                        doctor_output=None,
                        latency_ms=latency_ms,
                        ok=ok,
                        error_type=error_type,
                    )

        try:
            pool = ThreadPoolExecutor(max_workers=1)
            pool.submit(_run)
            pool.shutdown(wait=False)
        except Exception as exc:
            _log.warning("Could not launch critic thread: %s", exc)

    def _build_response(
        self,
        doctor_output: DoctorTurnOutput,
        critique: TurnCritique | None,
    ) -> dict:
        critique_data = None
        if critique is not None:
            critique_data = {
                "question_quality_score": critique.question_quality_score,
                "differential_quality_score": critique.differential_quality_score,
                "reasoning_quality_score": critique.reasoning_quality_score,
                "confidence_calibration": critique.confidence_calibration,
                "weakness_category": critique.weakness_category,
                "would_have_asked": critique.would_have_asked,
                "rationale": critique.rationale,
            }
        return {
            "doctor_question": doctor_output.chosen_question,
            "differential": [
                {"dx": d.dx, "prob": d.prob}
                for d in doctor_output.current_differential
            ],
            "confidence_to_stop": doctor_output.confidence_to_stop,
            "session_complete": self.complete,
            "termination_reason": self.termination_reason,
            "critique": critique_data,
        }
