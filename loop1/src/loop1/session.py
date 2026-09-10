from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from rich.console import Console
from rich.prompt import Prompt

from loop1.closing_turn import generate_closing_turn
from loop1.compressor import compress_context
from loop1.config import config
from loop1.doctor import generate_turn_with_usage
from loop1.logging_utils import log_event, write_final_record
from loop1.profile_updater import apply_delta, extract_profile_delta
from loop1.safety import EMERGENCY_MESSAGE, check_safety
from loop1.schemas import (
    ClosingTurn,
    DiagnosisEntry,
    DoctorTurnOutput,
    FinalRecord,
    ModelMetadata,
    PatientProfile,
    SafetyEvent,
    TurnRecord,
)

try:
    from admin_portal.instrumentation import usage_logger as _usage_logger
    # This module is the CLI/offline harness (phase7_eval.py, scripts/run_session.py) —
    # never the live web path (see loop1/web/api_session.py for that). Set the
    # default here, via setdefault, so a value already set by the caller's own
    # environment is never overridden.
    os.environ.setdefault("DIFFDX_RUN_CONTEXT", "offline_cli")
except Exception:          # admin_portal is optional; the session path must run without it
    _usage_logger = None

_log = logging.getLogger(__name__)

_DISCLAIMER = (
    "[bold yellow]PROTOTYPE[/bold yellow] — "
    "This is a research tool, not medical advice. "
    "Do not use for clinical decisions."
)

_DOCTOR_STYLE = "bold cyan"
_PROMPT_STYLE = "bold green"


class Session:
    def __init__(
        self,
        profile: PatientProfile,
        max_turns: int | None = None,
        confidence_threshold: float | None = None,
        patient_source: Any | None = None,
    ) -> None:
        """
        patient_source: optional object with .answer(question: str) -> str.
        If None, reads answers from the terminal interactively.
        """
        self.profile = profile
        self.history: list[TurnRecord] = []
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.max_turns: int = max_turns if max_turns is not None else config["thresholds"]["max_turns"]
        self.confidence_threshold: float = (
            confidence_threshold
            if confidence_threshold is not None
            else config["thresholds"]["confidence_to_stop"]
        )
        self.keep_recent: int = config["thresholds"]["compression_keep_recent"]
        self.patient_source = patient_source
        self.console = Console()

    def _recent_history(self) -> list[TurnRecord]:
        """Return the history slice to pass to the doctor (trimmed when compression is active)."""
        if self.profile.running_summary:
            return self.history[-self.keep_recent :]
        return self.history

    def _generate_turn_with_usage_logged(self, turn_index: int, call_site: str):
        """Wraps generate_turn_with_usage with latency timing and a usage-log
        call (admin_portal, optional; source=offline_cli via this module's
        own DIFFDX_RUN_CONTEXT default). Returns its result completely
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
                doctor_output, prompt_tokens = (result[0], result[1]) if result is not None else (None, None)
                _usage_logger.log_turn_usage(
                    session_id=self.profile.session_id,
                    turn_index=turn_index,
                    call_site=call_site,
                    prompt_tokens=prompt_tokens,
                    doctor_output=doctor_output,
                    latency_ms=latency_ms,
                    ok=ok,
                    error_type=error_type,
                )

    def _maybe_compress(self) -> None:
        """Trigger compression when there are turns older than the keep_recent window."""
        if len(self.history) <= self.keep_recent:
            return
        self.profile = compress_context(self.profile, self.history, self.keep_recent)
        log_event(
            session_id=self.profile.session_id,
            event_type="compression_complete",
            data={
                "turns_summarized": len(self.history) - self.keep_recent,
                "running_summary_length": len(self.profile.running_summary),
            },
        )

    def _log_turn(self, turn_record: TurnRecord, prompt_tokens: int) -> None:
        log_event(
            session_id=self.profile.session_id,
            event_type="turn_complete",
            data={
                "turn_index": turn_record.turn_index,
                "doctor_output": turn_record.doctor_output.model_dump(),
                "patient_answer": turn_record.patient_answer,
                "retrieved_exemplar_ids": turn_record.retrieved_exemplar_ids,
                "prompt_tokens": prompt_tokens,
                "timestamp": turn_record.timestamp,
                "profile_state": self.profile.model_dump(),
            },
        )

    def _log_session_end(
        self,
        termination_reason: str,
        final_doctor_output: DoctorTurnOutput,
    ) -> None:
        log_event(
            session_id=self.profile.session_id,
            event_type="session_end",
            data={
                "termination_reason": termination_reason,
                "doctor_output": final_doctor_output.model_dump(),
            },
        )

    def _log_safety_event(self, turn: int, matched_phrase: str, patient_input: str) -> None:
        event = SafetyEvent(
            session_id=self.profile.session_id,
            turn=turn,
            matched_phrase=matched_phrase,
            patient_input=patient_input,
        )
        log_event(
            session_id=self.profile.session_id,
            event_type="safety_event",
            data=event.model_dump(),
        )

    def _build_final_record(
        self,
        termination_reason: str,
        final_differential: list[DiagnosisEntry],
        closing_turn: ClosingTurn | None,
    ) -> FinalRecord:
        primary = final_differential[0].dx if final_differential else "undetermined"
        return FinalRecord(
            session_id=self.profile.session_id,
            started_at=self.started_at,
            ended_at=datetime.now(timezone.utc).isoformat(),
            termination_reason=termination_reason,  # type: ignore[arg-type]
            final_profile=self.profile,
            final_differential=final_differential,
            primary_diagnosis=primary,
            turn_history=self.history,
            model_metadata=ModelMetadata(
                model_name=config["models"]["doctor"],
                model_version="0",
                prompt_template_version=config["prompt_versions"]["doctor"],
            ),
            closing_turn=closing_turn,
        )

    def _generate_closing_turn(
        self, final_differential: list[DiagnosisEntry]
    ) -> ClosingTurn | None:
        closing = generate_closing_turn(self.profile, final_differential)
        if closing is None:
            _log.warning("Closing turn generation failed; continuing without it.")
            log_event(
                session_id=self.profile.session_id,
                event_type="closing_turn_failed",
                data={"reason": "generate_closing_turn returned None"},
            )
        else:
            log_event(
                session_id=self.profile.session_id,
                event_type="closing_turn_complete",
                data=closing.model_dump(),
            )
        return closing

    def run(self) -> FinalRecord:
        self.console.print(_DISCLAIMER)
        self.console.print()

        log_event(
            session_id=self.profile.session_id,
            event_type="session_start",
            data={
                "session_id": self.profile.session_id,
                "chief_complaint": self.profile.chief_complaint,
                "max_turns": self.max_turns,
                "confidence_threshold": self.confidence_threshold,
            },
        )

        termination_reason = "max_turns"
        final_doctor_output: DoctorTurnOutput | None = None

        for turn_index in range(self.max_turns):
            self.console.print(f"[dim]Turn {turn_index + 1} / {self.max_turns}[/dim]")

            doctor_output, prompt_tokens, exemplar_ids = self._generate_turn_with_usage_logged(
                turn_index, "next_question"
            )
            final_doctor_output = doctor_output

            if doctor_output.safety_flags:
                for flag in doctor_output.safety_flags:
                    self.console.print(f"[bold red]SAFETY ALERT:[/bold red] {flag}")
                termination_reason = "safety_stop"
                self._log_session_end(termination_reason, doctor_output)
                break

            if (
                doctor_output.should_stop
                or doctor_output.confidence_to_stop >= self.confidence_threshold
            ):
                termination_reason = "confidence_threshold"
                self._log_session_end(termination_reason, doctor_output)
                break

            self.console.print(f"\n[{_DOCTOR_STYLE}]Doctor:[/{_DOCTOR_STYLE}] {doctor_output.chosen_question}\n")

            if self.patient_source is not None:
                answer = self.patient_source.answer(doctor_output.chosen_question)
                self.console.print(f"[dim]Patient (simulated):[/dim] {answer}\n")
            else:
                answer = Prompt.ask(f"[{_PROMPT_STYLE}]Your answer[/{_PROMPT_STYLE}]")

            if answer.strip().lower() in ("quit", "q", "exit"):
                termination_reason = "user_quit"
                self._log_session_end(termination_reason, doctor_output)
                break

            # Safety check on patient input
            matched_phrase = check_safety(answer)
            if matched_phrase:
                self.console.print(f"\n[bold red]{EMERGENCY_MESSAGE}[/bold red]\n")
                self._log_safety_event(turn_index, matched_phrase, answer)
                termination_reason = "safety_stop"
                self._log_session_end(termination_reason, doctor_output)
                break

            turn_record = TurnRecord(
                turn_index=turn_index,
                doctor_output=doctor_output,
                patient_answer=answer,
                retrieved_exemplar_ids=exemplar_ids,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            self._log_turn(turn_record, prompt_tokens)
            self.history.append(turn_record)

            delta = extract_profile_delta(
                self.profile, doctor_output.chosen_question, answer
            )
            self.profile = apply_delta(self.profile, delta)

            self._maybe_compress()

            self.console.print()

        else:
            # Exhausted max_turns without a break — generate final assessment
            final_doctor_output, _, _exemplar_ids = self._generate_turn_with_usage_logged(
                self.max_turns, "final_generation"
            )
            self._log_session_end(termination_reason, final_doctor_output)

        assert final_doctor_output is not None

        # Generate closing turn (skipped for safety_stop)
        closing: ClosingTurn | None = None
        if termination_reason != "safety_stop":
            closing = self._generate_closing_turn(final_doctor_output.current_differential)

        final_record = self._build_final_record(
            termination_reason, final_doctor_output.current_differential, closing
        )

        path = write_final_record(
            self.profile.session_id, final_record.model_dump()
        )
        self.console.print(
            f"\n[bold]Session complete.[/bold] "
            f"Reason: [italic]{termination_reason}[/italic]. "
            f"Final record → {path}"
        )

        if closing:
            self.console.print("\n[bold cyan]Doctor's closing statement:[/bold cyan]")
            self.console.print(f"  {closing.leading_diagnosis}")
            self.console.print(f"  {closing.differential_summary}")
            if closing.recommended_next_steps:
                self.console.print("\n  [bold]Next steps:[/bold]")
                for step in closing.recommended_next_steps:
                    self.console.print(f"    • {step}")

        return final_record
