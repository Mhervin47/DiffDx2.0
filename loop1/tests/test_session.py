from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from loop1.llm import LlmUsage
from loop1.schemas import (
    Demographics,
    DiagnosisEntry,
    DoctorTurnOutput,
    History,
    PatientProfile,
    Symptom,
    TurnRecord,
)
from loop1.session import Session


def _usage(prompt_tokens: int) -> LlmUsage:
    return LlmUsage(prompt_tokens=prompt_tokens, completion_tokens=None, total_tokens=None, model_actual=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _profile(**kwargs) -> PatientProfile:
    defaults = dict(
        session_id="test-session-id",
        demographics=Demographics(age=35, sex="F"),
        chief_complaint="headache",
        symptoms=[Symptom(name="headache", onset="2 days ago")],
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
    safety_flags: list[str] | None = None,
) -> DoctorTurnOutput:
    return DoctorTurnOutput(
        turn_index=turn_index,
        current_differential=[DiagnosisEntry(dx="Migraine", prob=0.6)],
        biggest_uncertainty="onset trigger",
        candidate_questions=["What makes it worse?"],
        chosen_question="What makes it worse?",
        rationale="Need to differentiate migraine from tension headache.",
        confidence_to_stop=confidence,
        should_stop=should_stop,
        safety_flags=safety_flags or [],
    )


def _make_session(profile: PatientProfile | None = None, **kwargs) -> Session:
    return Session(profile=profile or _profile(), **kwargs)


# ---------------------------------------------------------------------------
# Token logging: prompt_tokens appears in turn log
# ---------------------------------------------------------------------------

def test_session_logs_prompt_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr("loop1.session.config", {
        "thresholds": {
            "max_turns": 3,
            "confidence_to_stop": 0.75,
            "compression_keep_recent": 3,
        },
        "models": {"doctor": "groq/test-model"},
        "prompt_versions": {"doctor": "v0.3"},
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "1.0",
        },
    })
    monkeypatch.setattr("loop1.logging_utils.config", {
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "1.0",
        },
    })

    turn0 = _doctor_output(0, confidence=0.2)
    turn1 = _doctor_output(1, confidence=0.9)  # triggers stop

    with (
        patch("loop1.session.generate_turn_with_usage", side_effect=[
            (turn0, _usage(512), []),
            (turn1, _usage(750), []),
        ]),
        patch("loop1.session.extract_profile_delta", return_value=(MagicMock(
            new_symptoms=[], symptom_updates=[], history_additions=MagicMock(
                medical=[], medications=[], allergies=[], family=[], social=[]
            ),
            append_ruled_out=[], append_ruled_in=[], free_notes_append="",
        ), None)),
        patch("loop1.session.apply_delta", side_effect=lambda p, d: p),
        patch("loop1.session.compress_context", side_effect=lambda p, h, k: (p, None)),
        patch("loop1.session.Prompt.ask", return_value="it gets worse with light"),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=None),
    ):
        session = _make_session()
        session.run()

    # Read the session log and find turn_complete events
    log_dir = tmp_path / "sessions"
    log_file = list(log_dir.glob("*.jsonl"))[0]
    events = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    turn_events = [e for e in events if e["event_type"] == "turn_complete"]

    assert len(turn_events) == 1
    assert turn_events[0]["prompt_tokens"] == 512


# ---------------------------------------------------------------------------
# Compression trigger: compress_context called after keep_recent+1 turns
# ---------------------------------------------------------------------------

def test_session_compression_not_triggered_before_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr("loop1.session.config", {
        "thresholds": {
            "max_turns": 10,
            "confidence_to_stop": 0.75,
            "compression_keep_recent": 3,
        },
        "models": {"doctor": "groq/test-model"},
        "prompt_versions": {"doctor": "v0.3"},
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "1.0",
        },
    })
    monkeypatch.setattr("loop1.logging_utils.config", {
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "1.0",
        },
    })

    # 3 turns then stop — never exceeds keep_recent=3 in history
    outputs = [
        _doctor_output(0, confidence=0.2),
        _doctor_output(1, confidence=0.2),
        _doctor_output(2, confidence=0.9),  # stop
    ]

    with (
        patch("loop1.session.generate_turn_with_usage", side_effect=[(o, _usage(400), []) for o in outputs]),
        patch("loop1.session.extract_profile_delta", return_value=(MagicMock(
            new_symptoms=[], symptom_updates=[], history_additions=MagicMock(
                medical=[], medications=[], allergies=[], family=[], social=[]
            ),
            append_ruled_out=[], append_ruled_in=[], free_notes_append="",
        ), None)),
        patch("loop1.session.apply_delta", side_effect=lambda p, d: p),
        patch("loop1.session.compress_context") as mock_compress,
        patch("loop1.session.Prompt.ask", return_value="some answer"),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=None),
    ):
        session = _make_session()
        session.run()

    mock_compress.assert_not_called()


def test_session_compression_triggered_after_keep_recent_plus_one(tmp_path, monkeypatch):
    monkeypatch.setattr("loop1.session.config", {
        "thresholds": {
            "max_turns": 10,
            "confidence_to_stop": 0.75,
            "compression_keep_recent": 3,
        },
        "models": {"doctor": "groq/test-model"},
        "prompt_versions": {"doctor": "v0.3"},
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "1.0",
        },
    })
    monkeypatch.setattr("loop1.logging_utils.config", {
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "1.0",
        },
    })

    # 4 answers → 4 turns in history → compression triggers after turn 3 answer
    outputs = [
        _doctor_output(0, confidence=0.2),
        _doctor_output(1, confidence=0.2),
        _doctor_output(2, confidence=0.2),
        _doctor_output(3, confidence=0.2),
        _doctor_output(4, confidence=0.9),  # stop
    ]
    profile = _profile()

    with (
        patch("loop1.session.generate_turn_with_usage", side_effect=[(o, _usage(400), []) for o in outputs]),
        patch("loop1.session.extract_profile_delta", return_value=(MagicMock(
            new_symptoms=[], symptom_updates=[], history_additions=MagicMock(
                medical=[], medications=[], allergies=[], family=[], social=[]
            ),
            append_ruled_out=[], append_ruled_in=[], free_notes_append="",
        ), None)),
        patch("loop1.session.apply_delta", side_effect=lambda p, d: p),
        patch("loop1.session.compress_context", return_value=(profile, None)) as mock_compress,
        patch("loop1.session.Prompt.ask", return_value="some answer"),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=None),
    ):
        session = _make_session()
        session.run()

    # compress_context was called (after turn 3 gives us 4 items in history)
    assert mock_compress.call_count >= 1


# ---------------------------------------------------------------------------
# History trimming: doctor receives only recent turns when compression active
# ---------------------------------------------------------------------------

def test_session_passes_full_history_before_compression(tmp_path, monkeypatch):
    monkeypatch.setattr("loop1.session.config", {
        "thresholds": {
            "max_turns": 10,
            "confidence_to_stop": 0.75,
            "compression_keep_recent": 3,
        },
        "models": {"doctor": "groq/test-model"},
        "prompt_versions": {"doctor": "v0.3"},
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "1.0",
        },
    })
    monkeypatch.setattr("loop1.logging_utils.config", {
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "1.0",
        },
    })

    outputs = [
        _doctor_output(0, confidence=0.2),
        _doctor_output(1, confidence=0.2),
        _doctor_output(2, confidence=0.9),  # stop after 2 answers (history len=2)
    ]
    history_args: list[list] = []

    def capture_generate(profile, history, turn_index):
        history_args.append(list(history))
        return outputs[turn_index], _usage(400), []

    with (
        patch("loop1.session.generate_turn_with_usage", side_effect=capture_generate),
        patch("loop1.session.extract_profile_delta", return_value=(MagicMock(
            new_symptoms=[], symptom_updates=[], history_additions=MagicMock(
                medical=[], medications=[], allergies=[], family=[], social=[]
            ),
            append_ruled_out=[], append_ruled_in=[], free_notes_append="",
        ), None)),
        patch("loop1.session.apply_delta", side_effect=lambda p, d: p),
        patch("loop1.session.compress_context", side_effect=lambda p, h, k: (p, None)),
        patch("loop1.session.Prompt.ask", return_value="some answer"),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=None),
    ):
        session = _make_session()
        session.run()

    # Before compression activates, full history is passed
    assert history_args[0] == []   # turn 0: no history yet
    assert len(history_args[1]) == 1  # turn 1: 1 prior turn
    assert len(history_args[2]) == 2  # turn 2 (stop check): 2 prior turns


def test_session_passes_trimmed_history_after_compression(tmp_path, monkeypatch):
    monkeypatch.setattr("loop1.session.config", {
        "thresholds": {
            "max_turns": 10,
            "confidence_to_stop": 0.75,
            "compression_keep_recent": 3,
        },
        "models": {"doctor": "groq/test-model"},
        "prompt_versions": {"doctor": "v0.3"},
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "1.0",
        },
    })
    monkeypatch.setattr("loop1.logging_utils.config", {
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "1.0",
        },
    })

    # 5 answers → compression triggers after turn 3; turn 4+ should get trimmed history
    outputs = [_doctor_output(i, confidence=0.2) for i in range(5)]
    outputs.append(_doctor_output(5, confidence=0.9))  # stop

    history_args: list[list] = []

    # compress_context sets running_summary to activate trimming
    def fake_compress(profile, history, keep_recent):
        return PatientProfile(
            session_id=profile.session_id,
            demographics=profile.demographics,
            chief_complaint=profile.chief_complaint,
            symptoms=profile.symptoms,
            history=profile.history,
            ruled_out=profile.ruled_out,
            ruled_in=profile.ruled_in,
            free_notes=profile.free_notes,
            running_summary="Summary of early turns.",
        ), None

    def capture_generate(profile, history, turn_index):
        history_args.append(list(history))
        idx = min(turn_index, len(outputs) - 1)
        return outputs[idx], _usage(400), []

    with (
        patch("loop1.session.generate_turn_with_usage", side_effect=capture_generate),
        patch("loop1.session.extract_profile_delta", return_value=(MagicMock(
            new_symptoms=[], symptom_updates=[], history_additions=MagicMock(
                medical=[], medications=[], allergies=[], family=[], social=[]
            ),
            append_ruled_out=[], append_ruled_in=[], free_notes_append="",
        ), None)),
        patch("loop1.session.apply_delta", side_effect=lambda p, d: p),
        patch("loop1.session.compress_context", side_effect=fake_compress),
        patch("loop1.session.Prompt.ask", return_value="some answer"),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=None),
    ):
        session = _make_session()
        session.run()

    # After compression (turn 4+), history passed to doctor should be <= keep_recent
    for i in range(4, len(history_args)):
        assert len(history_args[i]) <= 3, (
            f"Turn {i} received {len(history_args[i])} history items, expected <= 3"
        )


# ---------------------------------------------------------------------------
# compression_complete event logged
# ---------------------------------------------------------------------------

def test_session_logs_compression_complete_event(tmp_path, monkeypatch):
    monkeypatch.setattr("loop1.session.config", {
        "thresholds": {
            "max_turns": 10,
            "confidence_to_stop": 0.75,
            "compression_keep_recent": 3,
        },
        "models": {"doctor": "groq/test-model"},
        "prompt_versions": {"doctor": "v0.3"},
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "1.0",
        },
    })
    monkeypatch.setattr("loop1.logging_utils.config", {
        "logging": {
            "session_dir": str(tmp_path / "sessions"),
            "final_dir": str(tmp_path / "final"),
            "schema_version": "1.0",
        },
    })

    outputs = [_doctor_output(i, confidence=0.2) for i in range(4)]
    outputs.append(_doctor_output(4, confidence=0.9))

    def fake_compress(profile, history, keep_recent):
        return PatientProfile(
            session_id=profile.session_id,
            demographics=profile.demographics,
            chief_complaint=profile.chief_complaint,
            symptoms=profile.symptoms,
            history=profile.history,
            ruled_out=profile.ruled_out,
            ruled_in=profile.ruled_in,
            free_notes=profile.free_notes,
            running_summary="Summary.",
        ), None

    with (
        patch("loop1.session.generate_turn_with_usage", side_effect=[(o, _usage(400), []) for o in outputs]),
        patch("loop1.session.extract_profile_delta", return_value=(MagicMock(
            new_symptoms=[], symptom_updates=[], history_additions=MagicMock(
                medical=[], medications=[], allergies=[], family=[], social=[]
            ),
            append_ruled_out=[], append_ruled_in=[], free_notes_append="",
        ), None)),
        patch("loop1.session.apply_delta", side_effect=lambda p, d: p),
        patch("loop1.session.compress_context", side_effect=fake_compress),
        patch("loop1.session.Prompt.ask", return_value="some answer"),
        patch("loop1.session.write_final_record", return_value=tmp_path / "final.json"),
        patch("loop1.session.generate_closing_turn", return_value=None),
    ):
        session = _make_session()
        session.run()

    log_dir = tmp_path / "sessions"
    log_file = list(log_dir.glob("*.jsonl"))[0]
    events = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    compress_events = [e for e in events if e["event_type"] == "compression_complete"]
    assert len(compress_events) >= 1
    assert "turns_summarized" in compress_events[0]
    assert "running_summary_length" in compress_events[0]
