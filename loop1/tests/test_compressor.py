from __future__ import annotations

from unittest.mock import patch

import pytest

from loop1.compressor import (
    _dedup_list,
    _dedup_notes,
    _dedup_profile,
    _dedup_sentences,
    _dedup_symptoms,
    compress_context,
)
from loop1.llm import LlmUsage
from loop1.schemas import (
    Demographics,
    DoctorTurnOutput,
    History,
    PatientProfile,
    Symptom,
    TurnRecord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _profile(**kwargs) -> PatientProfile:
    defaults = dict(
        session_id="test-session",
        demographics=Demographics(age=40, sex="M"),
        chief_complaint="chest pain",
        symptoms=[],
        history=History(),
        ruled_out=[],
        ruled_in=[],
        free_notes="",
        running_summary="",
    )
    defaults.update(kwargs)
    return PatientProfile(**defaults)


def _doctor_output(turn_index: int = 0) -> DoctorTurnOutput:
    return DoctorTurnOutput(
        turn_index=turn_index,
        current_differential=[],
        biggest_uncertainty="unknown",
        candidate_questions=["q1"],
        chosen_question="q1",
        rationale="r",
        confidence_to_stop=0.2,
        should_stop=False,
        safety_flags=[],
    )


def _turn(turn_index: int = 0, answer: str = "yes") -> TurnRecord:
    return TurnRecord(
        turn_index=turn_index,
        doctor_output=_doctor_output(turn_index),
        patient_answer=answer,
        retrieved_exemplar_ids=[],
        timestamp="2026-01-01T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# _dedup_notes
# ---------------------------------------------------------------------------

def test_dedup_notes_removes_duplicate_phrases():
    result = _dedup_notes("sharp pain; sharp pain; radiates to arm")
    assert result == "sharp pain; radiates to arm"


def test_dedup_notes_case_insensitive():
    result = _dedup_notes("Sharp Pain; sharp pain")
    assert result == "Sharp Pain"


def test_dedup_notes_empty():
    assert _dedup_notes("") == ""


def test_dedup_notes_single_phrase():
    assert _dedup_notes("radiates to arm") == "radiates to arm"


# ---------------------------------------------------------------------------
# _dedup_sentences
# ---------------------------------------------------------------------------

def test_dedup_sentences_removes_duplicate_sentences():
    text = "Patient has chest pain. Patient has chest pain. Onset was sudden."
    result = _dedup_sentences(text)
    assert "Patient has chest pain." in result
    assert result.count("Patient has chest pain") == 1


def test_dedup_sentences_empty():
    assert _dedup_sentences("") == ""


def test_dedup_sentences_preserves_unique():
    text = "Onset was sudden. Pain radiates to the left arm."
    result = _dedup_sentences(text)
    assert "Onset was sudden" in result
    assert "Pain radiates" in result


# ---------------------------------------------------------------------------
# _dedup_list
# ---------------------------------------------------------------------------

def test_dedup_list_removes_exact_duplicates():
    assert _dedup_list(["asthma", "asthma", "diabetes"]) == ["asthma", "diabetes"]


def test_dedup_list_case_insensitive():
    result = _dedup_list(["Asthma", "asthma", "Diabetes"])
    assert len(result) == 2
    assert result[0] == "Asthma"


def test_dedup_list_empty():
    assert _dedup_list([]) == []


def test_dedup_list_no_duplicates():
    items = ["asthma", "diabetes", "hypertension"]
    assert _dedup_list(items) == items


# ---------------------------------------------------------------------------
# _dedup_symptoms — exact name matching
# ---------------------------------------------------------------------------

def test_dedup_symptoms_exact_case_insensitive():
    symptoms = [
        Symptom(name="Chest Pain", onset="2 days ago", severity="severe", notes=""),
        Symptom(name="chest pain", onset="", severity="moderate", notes="at rest"),
    ]
    result = _dedup_symptoms(symptoms)
    assert len(result) == 1
    assert result[0].name == "Chest Pain"
    assert result[0].onset == "2 days ago"
    assert result[0].severity == "severe"
    assert "at rest" in result[0].notes


def test_dedup_symptoms_merges_notes():
    symptoms = [
        Symptom(name="headache", notes="throbbing"),
        Symptom(name="headache", notes="worse in morning"),
    ]
    result = _dedup_symptoms(symptoms)
    assert len(result) == 1
    assert "throbbing" in result[0].notes
    assert "worse in morning" in result[0].notes


def test_dedup_symptoms_no_notes_duplication():
    symptoms = [
        Symptom(name="headache", notes="throbbing"),
        Symptom(name="headache", notes="throbbing"),
    ]
    result = _dedup_symptoms(symptoms)
    assert result[0].notes.count("throbbing") == 1


# ---------------------------------------------------------------------------
# _dedup_symptoms — substring / prefix merging
# ---------------------------------------------------------------------------

def test_dedup_symptoms_substring_merge_longer_into_shorter():
    symptoms = [
        Symptom(name="headache", onset="3 days ago", notes=""),
        Symptom(name="headache triggered by work", onset="", notes=""),
    ]
    result = _dedup_symptoms(symptoms)
    assert len(result) == 1
    assert result[0].name == "headache"
    assert "triggered by work" in result[0].notes


def test_dedup_symptoms_substring_merge_chest_pain():
    symptoms = [
        Symptom(name="chest pain", onset="1 day ago", severity="severe", notes=""),
        Symptom(name="chest pain at rest", onset="", severity="", notes=""),
    ]
    result = _dedup_symptoms(symptoms)
    assert len(result) == 1
    assert result[0].name == "chest pain"
    assert "at rest" in result[0].notes
    assert result[0].onset == "1 day ago"


def test_dedup_symptoms_non_prefix_substring_left_sided_chest_pain():
    """'chest pain' is a substring of 'left-sided chest pain' but not a prefix."""
    symptoms = [
        Symptom(name="chest pain", onset="2 hours ago", severity="severe", notes=""),
        Symptom(name="left-sided chest pain", onset="", severity="", notes=""),
    ]
    result = _dedup_symptoms(symptoms)
    assert len(result) == 1
    assert result[0].name == "chest pain"
    assert result[0].onset == "2 hours ago"


def test_dedup_symptoms_non_prefix_substring_episodes_of():
    """'shortness of breath' is a substring of 'episodes of shortness of breath'."""
    symptoms = [
        Symptom(name="shortness of breath", onset="1 day ago", notes=""),
        Symptom(name="episodes of shortness of breath", onset="", notes=""),
    ]
    result = _dedup_symptoms(symptoms)
    assert len(result) == 1
    assert result[0].name == "shortness of breath"
    assert result[0].onset == "1 day ago"


def test_dedup_symptoms_content_word_subset_leg_swelling():
    """'swelling in legs' and 'left leg swelling' share content words — merge."""
    symptoms = [
        Symptom(name="swelling in legs", onset="3 days ago", notes=""),
        Symptom(name="left leg swelling", onset="", notes=""),
    ]
    result = _dedup_symptoms(symptoms)
    assert len(result) == 1
    # The simpler/fewer-content-word name is kept
    assert result[0].onset == "3 days ago"


def test_dedup_symptoms_preserves_unrelated():
    symptoms = [
        Symptom(name="chest pain", notes=""),
        Symptom(name="shortness of breath", notes=""),
        Symptom(name="nausea", notes=""),
    ]
    result = _dedup_symptoms(symptoms)
    assert len(result) == 3


def test_dedup_symptoms_empty():
    assert _dedup_symptoms([]) == []


# ---------------------------------------------------------------------------
# _dedup_profile
# ---------------------------------------------------------------------------

def test_dedup_profile_deduplicates_all_fields():
    profile = _profile(
        symptoms=[
            Symptom(name="chest pain", notes="sharp"),
            Symptom(name="Chest Pain", notes="sharp"),
        ],
        history=History(
            medical=["asthma", "asthma", "diabetes"],
            social=["smoker", "Smoker"],
        ),
        free_notes="Patient is anxious. Patient is anxious.",
    )
    result = _dedup_profile(profile)
    assert len(result.symptoms) == 1
    assert len(result.history.medical) == 2
    assert len(result.history.social) == 1
    assert result.free_notes.count("Patient is anxious") == 1


def test_dedup_profile_preserves_session_id():
    profile = _profile()
    result = _dedup_profile(profile)
    assert result.session_id == "test-session"


def test_dedup_profile_preserves_running_summary():
    profile = _profile(running_summary="Prior summary.")
    result = _dedup_profile(profile)
    assert result.running_summary == "Prior summary."


# ---------------------------------------------------------------------------
# compress_context — no LLM needed (history <= keep_recent)
# ---------------------------------------------------------------------------

_USAGE = LlmUsage(prompt_tokens=100, completion_tokens=None, total_tokens=None, model_actual=None)


def test_compress_context_no_llm_when_history_short():
    profile = _profile()
    history = [_turn(0), _turn(1), _turn(2)]  # exactly keep_recent=3
    with patch("loop1.compressor._llm_recategorize_and_summarize") as mock_llm:
        result, usage = compress_context(profile, history, keep_recent=3)
    mock_llm.assert_not_called()
    assert result.running_summary == ""
    assert usage is None


def test_compress_context_no_llm_when_history_empty():
    profile = _profile()
    with patch("loop1.compressor._llm_recategorize_and_summarize") as mock_llm:
        compress_context(profile, [], keep_recent=3)
    mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# compress_context — LLM triggered when history > keep_recent
# ---------------------------------------------------------------------------

def test_compress_context_calls_llm_when_history_exceeds_keep_recent():
    profile = _profile()
    history = [_turn(i) for i in range(4)]
    corrected_history = History()
    with patch(
        "loop1.compressor._llm_recategorize_and_summarize",
        return_value=(corrected_history, "Patient confirmed chest pain onset 2 days ago.", _USAGE),
    ) as mock_llm:
        result, usage = compress_context(profile, history, keep_recent=3)
    mock_llm.assert_called_once()
    assert usage is _USAGE
    call_turns = mock_llm.call_args[0][1]
    assert len(call_turns) == 1
    assert call_turns[0].turn_index == 0


def test_compress_context_sets_running_summary():
    profile = _profile()
    history = [_turn(i) for i in range(5)]
    summary = "Patient reported sharp chest pain radiating to left arm, onset 2 days ago."
    with patch(
        "loop1.compressor._llm_recategorize_and_summarize",
        return_value=(History(), summary, _USAGE),
    ):
        result, usage = compress_context(profile, history, keep_recent=3)
    assert result.running_summary == summary


def test_compress_context_applies_corrected_history():
    profile = _profile(
        history=History(
            medical=["blood clots"],
            social=["blood clots"],
        )
    )
    history = [_turn(i) for i in range(5)]
    corrected_history = History(medical=["blood clots"], social=[])
    with patch(
        "loop1.compressor._llm_recategorize_and_summarize",
        return_value=(corrected_history, "Summary text.", _USAGE),
    ):
        result, usage = compress_context(profile, history, keep_recent=3)
    assert result.history.medical == ["blood clots"]
    assert result.history.social == []


def test_compress_context_dedup_runs_before_llm():
    profile = _profile(
        symptoms=[
            Symptom(name="chest pain", notes=""),
            Symptom(name="chest pain", notes="at rest"),
        ],
    )
    history = [_turn(i) for i in range(5)]
    captured: dict = {}

    def capture_call(cleaned_profile, turns):
        captured["symptoms"] = cleaned_profile.symptoms
        return History(), "Summary.", _USAGE

    with patch("loop1.compressor._llm_recategorize_and_summarize", side_effect=capture_call):
        compress_context(profile, history, keep_recent=3)

    assert len(captured["symptoms"]) == 1


def test_compress_context_passes_correct_turns_to_llm():
    profile = _profile()
    history = [_turn(i) for i in range(7)]  # turns 0-6, keep_recent=3 → compress turns 0-3
    captured: dict = {}

    def capture_call(p, turns):
        captured["turns"] = turns
        return History(), "Summary.", _USAGE

    with patch("loop1.compressor._llm_recategorize_and_summarize", side_effect=capture_call):
        compress_context(profile, history, keep_recent=3)

    assert len(captured["turns"]) == 4
    assert captured["turns"][0].turn_index == 0
    assert captured["turns"][-1].turn_index == 3
