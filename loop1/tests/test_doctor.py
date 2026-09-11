"""Phase 1 — unit tests for doctor.py using mocked LLM calls."""
import json
from unittest.mock import MagicMock, call, patch

import pytest

from loop1.doctor import build_doctor_prompt, generate_turn
from loop1.llm import LlmUsage
from loop1.schemas import (
    Demographics,
    DiagnosisEntry,
    DoctorTurnOutput,
    PatientProfile,
    Symptom,
    TurnRecord,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

VALID_OUTPUT: dict = {
    "turn_index": 0,
    "current_differential": [
        {"dx": "migraine", "prob": 0.55},
        {"dx": "tension headache", "prob": 0.30},
    ],
    "biggest_uncertainty": "Presence or absence of aura",
    "candidate_questions": [
        "Do you see any visual disturbances before the headache starts?",
        "Is the pain one-sided or spread across your whole head?",
    ],
    "chosen_question": "Do you see any flashing lights or blind spots before the headache begins?",
    "rationale": "Aura strongly supports migraine over tension headache.",
    "confidence_to_stop": 0.25,
    "should_stop": False,
    "safety_flags": [],
}

VALID_JSON = json.dumps(VALID_OUTPUT)

# call_llm_with_usage returns (content, LlmUsage)
_TOKEN_COUNT = 100


def _ok(content: str) -> tuple[str, LlmUsage]:
    return (content, LlmUsage(
        prompt_tokens=_TOKEN_COUNT, completion_tokens=None, total_tokens=None, model_actual=None,
    ))


def make_profile() -> PatientProfile:
    return PatientProfile(
        session_id="test-session-001",
        demographics=Demographics(age=34, sex="F"),
        chief_complaint="headache",
        symptoms=[Symptom(name="headache", onset="3 days ago", severity="moderate")],
    )


def make_turn_record(turn_index: int = 0) -> TurnRecord:
    doctor_output = DoctorTurnOutput(**{**VALID_OUTPUT, "turn_index": turn_index})
    return TurnRecord(
        turn_index=turn_index,
        doctor_output=doctor_output,
        patient_answer="Yes, I see zigzag lines before the headache.",
        retrieved_exemplar_ids=[],
        timestamp="2026-05-22T10:00:00+00:00",
    )


# ── build_doctor_prompt ───────────────────────────────────────────────────────

def test_build_prompt_contains_profile():
    msgs = build_doctor_prompt(make_profile(), [], turn_index=0)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert "headache" in msgs[1]["content"]
    assert "turn_index 0" in msgs[1]["content"]


def test_build_prompt_no_history_section_when_empty():
    msgs = build_doctor_prompt(make_profile(), [], turn_index=0)
    assert "Conversation history" not in msgs[1]["content"]


def test_build_prompt_includes_history_when_present():
    history = [make_turn_record(turn_index=0)]
    msgs = build_doctor_prompt(make_profile(), history, turn_index=1)
    user_msg = msgs[1]["content"]
    assert "Conversation history" in user_msg
    assert "Do you see any flashing lights" in user_msg
    assert "Yes, I see zigzag lines" in user_msg
    assert "turn_index 1" in user_msg


def test_build_prompt_system_prompt_loaded():
    msgs = build_doctor_prompt(make_profile(), [], turn_index=0)
    assert len(msgs[0]["content"]) > 100  # non-trivial system prompt


# ── generate_turn: happy path ─────────────────────────────────────────────────

def test_valid_json_on_first_attempt():
    with patch("loop1.doctor.call_llm_with_usage", return_value=_ok(VALID_JSON)) as mock_llm:
        result = generate_turn(make_profile())
    assert isinstance(result, DoctorTurnOutput)
    assert result.turn_index == 0
    assert result.chosen_question == VALID_OUTPUT["chosen_question"]
    assert result.should_stop is False
    mock_llm.assert_called_once()


def test_markdown_fences_stripped():
    fenced = f"```json\n{VALID_JSON}\n```"
    with patch("loop1.doctor.call_llm_with_usage", return_value=_ok(fenced)):
        result = generate_turn(make_profile())
    assert isinstance(result, DoctorTurnOutput)


def test_turn_index_passed_through():
    output = {**VALID_OUTPUT, "turn_index": 3}
    with patch("loop1.doctor.call_llm_with_usage", return_value=_ok(json.dumps(output))):
        result = generate_turn(make_profile(), turn_index=3)
    assert result.turn_index == 3


def test_history_forwarded_to_prompt():
    history = [make_turn_record()]
    with patch("loop1.doctor.call_llm_with_usage", return_value=_ok(VALID_JSON)) as mock_llm:
        generate_turn(make_profile(), history=history, turn_index=1)
    user_content = mock_llm.call_args[1]["messages"][1]["content"]
    assert "Conversation history" in user_content


# ── generate_turn: retry on bad JSON ─────────────────────────────────────────

def test_invalid_json_then_valid_succeeds():
    responses = [_ok("not json at all }{"), _ok(VALID_JSON)]
    with patch("loop1.doctor.call_llm_with_usage", side_effect=responses) as mock_llm:
        result = generate_turn(make_profile(), max_retries=3)
    assert isinstance(result, DoctorTurnOutput)
    assert mock_llm.call_count == 2


def test_retry_message_contains_parse_error():
    """After a bad JSON response, the retry user message must mention the error."""
    call_count = 0

    def side_effect(model, messages, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _ok("this is not json")
        assert any(
            "not valid JSON" in m["content"]
            for m in messages
            if m["role"] == "user"
        ), "Retry messages should contain JSON error feedback"
        return _ok(VALID_JSON)

    with patch("loop1.doctor.call_llm_with_usage", side_effect=side_effect):
        result = generate_turn(make_profile(), max_retries=3)
    assert isinstance(result, DoctorTurnOutput)


# ── generate_turn: retry on Pydantic validation failure ──────────────────────

def test_invalid_schema_then_valid_succeeds():
    bad_output = {**VALID_OUTPUT, "confidence_to_stop": 99.0}  # out of [0,1]
    responses = [_ok(json.dumps(bad_output)), _ok(VALID_JSON)]
    with patch("loop1.doctor.call_llm_with_usage", side_effect=responses) as mock_llm:
        result = generate_turn(make_profile(), max_retries=3)
    assert isinstance(result, DoctorTurnOutput)
    assert mock_llm.call_count == 2


def test_retry_message_contains_validation_error():
    """After a schema violation, the retry message must mention validation errors."""
    call_count = 0
    bad_output = {**VALID_OUTPUT, "confidence_to_stop": -5.0}

    def side_effect(model, messages, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _ok(json.dumps(bad_output))
        assert any(
            "schema validation" in m["content"]
            for m in messages
            if m["role"] == "user"
        ), "Retry messages should contain schema validation feedback"
        return _ok(VALID_JSON)

    with patch("loop1.doctor.call_llm_with_usage", side_effect=side_effect):
        result = generate_turn(make_profile(), max_retries=3)
    assert isinstance(result, DoctorTurnOutput)


# ── generate_turn: persistent failure ────────────────────────────────────────

def test_persistent_bad_json_raises_after_max_retries():
    with patch("loop1.doctor.call_llm_with_usage", return_value=_ok("}{bad json")):
        with pytest.raises(ValueError, match="Failed to get a valid DoctorTurnOutput"):
            generate_turn(make_profile(), max_retries=3)


def test_persistent_schema_violation_raises_after_max_retries():
    bad = {**VALID_OUTPUT, "turn_index": -1}  # negative index rejected by schema
    with patch("loop1.doctor.call_llm_with_usage", return_value=_ok(json.dumps(bad))):
        with pytest.raises(ValueError, match="Failed to get a valid DoctorTurnOutput"):
            generate_turn(make_profile(), max_retries=3)


def test_exact_retry_count_on_persistent_failure():
    with patch("loop1.doctor.call_llm_with_usage", return_value=_ok("not json")) as mock_llm:
        with pytest.raises(ValueError):
            generate_turn(make_profile(), max_retries=3)
    assert mock_llm.call_count == 3
