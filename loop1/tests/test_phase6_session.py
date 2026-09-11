"""Phase 6 — session safety termination, closing turn, profile_state logging."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from loop1.llm import LlmUsage
from loop1.schemas import (
    Demographics,
    DiagnosisEntry,
    DoctorTurnOutput,
    History,
    PatientProfile,
    Symptom,
)
from loop1.session import Session


def _usage(prompt_tokens: int = 400) -> LlmUsage:
    return LlmUsage(prompt_tokens=prompt_tokens, completion_tokens=None, total_tokens=None, model_actual=None)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _profile(**kwargs) -> PatientProfile:
    defaults = dict(
        session_id="test-p6-session",
        demographics=Demographics(age=28, sex="F"),
        chief_complaint="rash",
        symptoms=[Symptom(name="rash", onset="2 days ago")],
        history=History(),
        ruled_out=[],
        ruled_in=[],
        free_notes="",
        running_summary="",
    )
    defaults.update(kwargs)
    return PatientProfile(**defaults)


def _doctor_output(
    turn_index: int = 0,
    confidence: float = 0.2,
    should_stop: bool = False,
) -> DoctorTurnOutput:
    return DoctorTurnOutput(
        turn_index=turn_index,
        current_differential=[DiagnosisEntry(dx="Eczema", prob=0.5)],
        biggest_uncertainty="trigger",
        candidate_questions=["Is it itchy?"],
        chosen_question="Is it itchy?",
        rationale="Pruritus distinguishes eczema from psoriasis.",
        confidence_to_stop=confidence,
        should_stop=should_stop,
        safety_flags=[],
    )


def _session_config(tmp_path: Path) -> dict:
    return {
        "thresholds": {
            "max_turns": 5,
            "confidence_to_stop": 0.75,
            "compression_keep_recent": 3,
        },
        "models": {"doctor": "groq/test-model"},
        "prompt_versions": {"doctor": "v0.4"},
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "0.5.0",
        },
    }


def _mock_delta():
    return MagicMock(
        new_symptoms=[],
        symptom_updates=[],
        history_additions=MagicMock(
            medical=[], medications=[], allergies=[], family=[], social=[]
        ),
        append_ruled_out=[],
        append_ruled_in=[],
        free_notes_append="",
    )


# ---------------------------------------------------------------------------
# Safety termination: emergency phrase in patient input
# ---------------------------------------------------------------------------

def test_safety_terminates_session_on_emergency_phrase(tmp_path, monkeypatch):
    cfg = _session_config(tmp_path)
    monkeypatch.setattr("loop1.session.config", cfg)
    monkeypatch.setattr("loop1.logging_utils.config", {"logging": cfg["logging"]})

    turn0 = _doctor_output(0, confidence=0.2)

    with (
        patch("loop1.session.generate_turn_with_usage", return_value=(turn0, _usage(), [])),
        patch("loop1.session.extract_profile_delta", return_value=(_mock_delta(), None)),
        patch("loop1.session.apply_delta", side_effect=lambda p, d: p),
        patch("loop1.session.compress_context", side_effect=lambda p, h, k: (p, None)),
        patch("loop1.session.Prompt.ask", return_value="I am having a seizure"),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=None),
    ):
        session = Session(profile=_profile())
        record = session.run()

    assert record.termination_reason == "safety_stop"


def test_safety_logs_safety_event(tmp_path, monkeypatch):
    cfg = _session_config(tmp_path)
    monkeypatch.setattr("loop1.session.config", cfg)
    monkeypatch.setattr("loop1.logging_utils.config", {"logging": cfg["logging"]})

    turn0 = _doctor_output(0, confidence=0.2)

    with (
        patch("loop1.session.generate_turn_with_usage", return_value=(turn0, _usage(), [])),
        patch("loop1.session.extract_profile_delta", return_value=(_mock_delta(), None)),
        patch("loop1.session.apply_delta", side_effect=lambda p, d: p),
        patch("loop1.session.compress_context", side_effect=lambda p, h, k: (p, None)),
        patch("loop1.session.Prompt.ask", return_value="I am suicidal"),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=None),
    ):
        session = Session(profile=_profile())
        session.run()

    log_dir = tmp_path / "sessions"
    log_file = list(log_dir.glob("*.jsonl"))[0]
    events = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    safety_events = [e for e in events if e["event_type"] == "safety_event"]

    assert len(safety_events) == 1
    assert safety_events[0]["matched_phrase"] == "suicidal"
    assert "patient_input" in safety_events[0]
    assert safety_events[0]["action"] == "session_terminated"


def test_safety_does_not_generate_closing_turn(tmp_path, monkeypatch):
    cfg = _session_config(tmp_path)
    monkeypatch.setattr("loop1.session.config", cfg)
    monkeypatch.setattr("loop1.logging_utils.config", {"logging": cfg["logging"]})

    turn0 = _doctor_output(0, confidence=0.2)

    with (
        patch("loop1.session.generate_turn_with_usage", return_value=(turn0, _usage(), [])),
        patch("loop1.session.extract_profile_delta", return_value=(_mock_delta(), None)),
        patch("loop1.session.apply_delta", side_effect=lambda p, d: p),
        patch("loop1.session.compress_context", side_effect=lambda p, h, k: (p, None)),
        patch("loop1.session.Prompt.ask", return_value="I want to die"),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn") as mock_closing,
    ):
        session = Session(profile=_profile())
        session.run()

    mock_closing.assert_not_called()


def test_normal_input_does_not_trigger_safety(tmp_path, monkeypatch):
    cfg = _session_config(tmp_path)
    monkeypatch.setattr("loop1.session.config", cfg)
    monkeypatch.setattr("loop1.logging_utils.config", {"logging": cfg["logging"]})

    turn0 = _doctor_output(0, confidence=0.2)
    turn1 = _doctor_output(1, confidence=0.9)  # triggers confidence stop

    with (
        patch("loop1.session.generate_turn_with_usage", side_effect=[
            (turn0, _usage(), []),
            (turn1, _usage(), []),
        ]),
        patch("loop1.session.extract_profile_delta", return_value=(_mock_delta(), None)),
        patch("loop1.session.apply_delta", side_effect=lambda p, d: p),
        patch("loop1.session.compress_context", side_effect=lambda p, h, k: (p, None)),
        patch("loop1.session.Prompt.ask", return_value="I have a mild rash"),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=None),
    ):
        session = Session(profile=_profile())
        record = session.run()

    assert record.termination_reason == "confidence_threshold"


# ---------------------------------------------------------------------------
# Closing turn: generated on normal termination, logged, present in FinalRecord
# ---------------------------------------------------------------------------

def test_closing_turn_generated_on_confidence_stop(tmp_path, monkeypatch):
    from loop1.schemas import ClosingTurn
    cfg = _session_config(tmp_path)
    monkeypatch.setattr("loop1.session.config", cfg)
    monkeypatch.setattr("loop1.logging_utils.config", {"logging": cfg["logging"]})

    turn0 = _doctor_output(0, confidence=0.2)
    turn1 = _doctor_output(1, confidence=0.9)

    mock_ct = ClosingTurn(
        leading_diagnosis="Most likely eczema.",
        differential_summary="Eczema (50%).",
        recommended_next_steps=["See a dermatologist"],
        unresolved_patient_questions=[],
        generated_by="groq/test",
    )

    with (
        patch("loop1.session.generate_turn_with_usage", side_effect=[
            (turn0, _usage(), []),
            (turn1, _usage(), []),
        ]),
        patch("loop1.session.extract_profile_delta", return_value=(_mock_delta(), None)),
        patch("loop1.session.apply_delta", side_effect=lambda p, d: p),
        patch("loop1.session.compress_context", side_effect=lambda p, h, k: (p, None)),
        patch("loop1.session.Prompt.ask", return_value="Yes it itches"),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=mock_ct),
    ):
        session = Session(profile=_profile())
        record = session.run()

    assert record.closing_turn is not None
    assert record.closing_turn.leading_diagnosis == "Most likely eczema."


def test_closing_turn_logged_to_jsonl(tmp_path, monkeypatch):
    from loop1.schemas import ClosingTurn
    cfg = _session_config(tmp_path)
    monkeypatch.setattr("loop1.session.config", cfg)
    monkeypatch.setattr("loop1.logging_utils.config", {"logging": cfg["logging"]})

    turn0 = _doctor_output(0, confidence=0.9)  # immediate stop

    mock_ct = ClosingTurn(
        leading_diagnosis="Most likely eczema.",
        differential_summary="Eczema (50%).",
        recommended_next_steps=[],
        unresolved_patient_questions=[],
        generated_by="groq/test",
    )

    with (
        patch("loop1.session.generate_turn_with_usage", return_value=(turn0, _usage(), [])),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=mock_ct),
    ):
        session = Session(profile=_profile())
        session.run()

    log_dir = tmp_path / "sessions"
    log_file = list(log_dir.glob("*.jsonl"))[0]
    events = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    ct_events = [e for e in events if e["event_type"] == "closing_turn_complete"]

    assert len(ct_events) == 1
    assert ct_events[0]["leading_diagnosis"] == "Most likely eczema."


def test_closing_turn_failure_does_not_crash(tmp_path, monkeypatch):
    cfg = _session_config(tmp_path)
    monkeypatch.setattr("loop1.session.config", cfg)
    monkeypatch.setattr("loop1.logging_utils.config", {"logging": cfg["logging"]})

    turn0 = _doctor_output(0, confidence=0.9)

    with (
        patch("loop1.session.generate_turn_with_usage", return_value=(turn0, _usage(), [])),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=None),  # failure
    ):
        session = Session(profile=_profile())
        record = session.run()

    assert record.closing_turn is None  # graceful — no crash


# ---------------------------------------------------------------------------
# profile_state logged in turn_complete events
# ---------------------------------------------------------------------------

def test_turn_complete_logs_profile_state(tmp_path, monkeypatch):
    cfg = _session_config(tmp_path)
    monkeypatch.setattr("loop1.session.config", cfg)
    monkeypatch.setattr("loop1.logging_utils.config", {"logging": cfg["logging"]})

    turn0 = _doctor_output(0, confidence=0.2)
    turn1 = _doctor_output(1, confidence=0.9)

    with (
        patch("loop1.session.generate_turn_with_usage", side_effect=[
            (turn0, _usage(), []),
            (turn1, _usage(), []),
        ]),
        patch("loop1.session.extract_profile_delta", return_value=(_mock_delta(), None)),
        patch("loop1.session.apply_delta", side_effect=lambda p, d: p),
        patch("loop1.session.compress_context", side_effect=lambda p, h, k: (p, None)),
        patch("loop1.session.Prompt.ask", return_value="It is itchy"),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=None),
    ):
        session = Session(profile=_profile())
        session.run()

    log_dir = tmp_path / "sessions"
    log_file = list(log_dir.glob("*.jsonl"))[0]
    events = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    turn_events = [e for e in events if e["event_type"] == "turn_complete"]

    assert len(turn_events) == 1
    assert "profile_state" in turn_events[0]
    assert "session_id" in turn_events[0]["profile_state"]
