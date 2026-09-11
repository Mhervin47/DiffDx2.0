"""Phase 2 — unit tests for profile_updater.py (apply_delta is pure, no mocking needed)."""
import json
from unittest.mock import patch

import pytest

from loop1.llm import LlmUsage
from loop1.profile_updater import apply_delta, extract_profile_delta
from loop1.schemas import (
    Demographics,
    HistoryAdditions,
    PatientProfile,
    ProfileDelta,
    Symptom,
    SymptomUpdate,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def make_profile(
    symptoms=None,
    medical=None,
    medications=None,
    allergies=None,
    family=None,
    social=None,
    ruled_out=None,
    ruled_in=None,
    free_notes="",
) -> PatientProfile:
    from loop1.schemas import History

    return PatientProfile(
        session_id="test-001",
        demographics=Demographics(age=34, sex="F"),
        chief_complaint="headache",
        symptoms=symptoms or [
            Symptom(name="headache", onset="3 days ago", severity="moderate")
        ],
        history=History(
            medical=medical or [],
            medications=medications or [],
            allergies=allergies or [],
            family=family or [],
            social=social or [],
        ),
        ruled_out=ruled_out or [],
        ruled_in=ruled_in or [],
        free_notes=free_notes,
    )


def empty_delta(**overrides) -> ProfileDelta:
    defaults = dict(
        new_symptoms=[],
        symptom_updates=[],
        history_additions=HistoryAdditions(),
        append_ruled_out=[],
        append_ruled_in=[],
        free_notes_append="",
    )
    defaults.update(overrides)
    return ProfileDelta(**defaults)


# ── apply_delta: new symptoms ─────────────────────────────────────────────────


def test_apply_delta_adds_new_symptom():
    profile = make_profile()
    delta = empty_delta(new_symptoms=[Symptom(name="nausea", onset="today")])
    result = apply_delta(profile, delta)
    names = [s.name for s in result.symptoms]
    assert "nausea" in names
    assert "headache" in names


def test_apply_delta_does_not_duplicate_existing_symptom():
    profile = make_profile()
    # "headache" already exists in the fixture
    delta = empty_delta(new_symptoms=[Symptom(name="headache", notes="extra")])
    result = apply_delta(profile, delta)
    assert sum(1 for s in result.symptoms if s.name == "headache") == 1


def test_apply_delta_case_insensitive_dedup():
    profile = make_profile()
    delta = empty_delta(new_symptoms=[Symptom(name="Headache", onset="yesterday")])
    result = apply_delta(profile, delta)
    assert sum(1 for s in result.symptoms if s.name.lower() == "headache") == 1


# ── apply_delta: symptom updates ─────────────────────────────────────────────


def test_apply_delta_updates_existing_symptom_notes():
    profile = make_profile()
    delta = empty_delta(
        symptom_updates=[SymptomUpdate(name="headache", notes="preceded by aura")]
    )
    result = apply_delta(profile, delta)
    updated = next(s for s in result.symptoms if s.name == "headache")
    assert "preceded by aura" in updated.notes


def test_apply_delta_updates_onset():
    profile = make_profile()
    delta = empty_delta(
        symptom_updates=[SymptomUpdate(name="headache", onset="2 days ago")]
    )
    result = apply_delta(profile, delta)
    updated = next(s for s in result.symptoms if s.name == "headache")
    assert updated.onset == "2 days ago"


def test_apply_delta_update_preserves_existing_onset_when_empty():
    profile = make_profile()  # headache onset="3 days ago"
    delta = empty_delta(
        symptom_updates=[SymptomUpdate(name="headache", onset="")]
    )
    result = apply_delta(profile, delta)
    updated = next(s for s in result.symptoms if s.name == "headache")
    assert updated.onset == "3 days ago"


def test_apply_delta_appends_notes_to_existing():
    profile = make_profile(
        symptoms=[Symptom(name="headache", onset="3 days ago", notes="throbbing")]
    )
    delta = empty_delta(
        symptom_updates=[SymptomUpdate(name="headache", notes="worse with light")]
    )
    result = apply_delta(profile, delta)
    updated = next(s for s in result.symptoms if s.name == "headache")
    assert "throbbing" in updated.notes
    assert "worse with light" in updated.notes


# ── apply_delta: history ─────────────────────────────────────────────────────


def test_apply_delta_adds_medication():
    profile = make_profile()
    delta = empty_delta(
        history_additions=HistoryAdditions(medications=["ibuprofen 400 mg"])
    )
    result = apply_delta(profile, delta)
    assert "ibuprofen 400 mg" in result.history.medications


def test_apply_delta_does_not_duplicate_medication():
    profile = make_profile(medications=["ibuprofen 400 mg"])
    delta = empty_delta(
        history_additions=HistoryAdditions(medications=["ibuprofen 400 mg"])
    )
    result = apply_delta(profile, delta)
    assert result.history.medications.count("ibuprofen 400 mg") == 1


def test_apply_delta_adds_family_history():
    profile = make_profile()
    delta = empty_delta(
        history_additions=HistoryAdditions(family=["father: MI at 55"])
    )
    result = apply_delta(profile, delta)
    assert "father: MI at 55" in result.history.family


def test_apply_delta_adds_medical_history():
    profile = make_profile()
    delta = empty_delta(
        history_additions=HistoryAdditions(medical=["hypertension"])
    )
    result = apply_delta(profile, delta)
    assert "hypertension" in result.history.medical


# ── apply_delta: ruled_out / ruled_in ────────────────────────────────────────


def test_apply_delta_appends_ruled_out():
    profile = make_profile()
    delta = empty_delta(append_ruled_out=["tension headache"])
    result = apply_delta(profile, delta)
    assert "tension headache" in result.ruled_out


def test_apply_delta_does_not_duplicate_ruled_out():
    profile = make_profile(ruled_out=["tension headache"])
    delta = empty_delta(append_ruled_out=["tension headache"])
    result = apply_delta(profile, delta)
    assert result.ruled_out.count("tension headache") == 1


def test_apply_delta_appends_ruled_in():
    profile = make_profile()
    delta = empty_delta(append_ruled_in=["migraine"])
    result = apply_delta(profile, delta)
    assert "migraine" in result.ruled_in


# ── apply_delta: free_notes ───────────────────────────────────────────────────


def test_apply_delta_appends_free_notes_when_existing():
    profile = make_profile(free_notes="Initial note.")
    delta = empty_delta(free_notes_append="Second note.")
    result = apply_delta(profile, delta)
    assert "Initial note." in result.free_notes
    assert "Second note." in result.free_notes


def test_apply_delta_sets_free_notes_when_previously_empty():
    profile = make_profile(free_notes="")
    delta = empty_delta(free_notes_append="First note.")
    result = apply_delta(profile, delta)
    assert result.free_notes == "First note."


def test_apply_delta_empty_append_leaves_free_notes_unchanged():
    profile = make_profile(free_notes="Existing note.")
    delta = empty_delta(free_notes_append="")
    result = apply_delta(profile, delta)
    assert result.free_notes == "Existing note."


# ── apply_delta: purity ───────────────────────────────────────────────────────


def test_apply_delta_does_not_mutate_original_profile():
    profile = make_profile()
    original_symptom_count = len(profile.symptoms)
    delta = empty_delta(new_symptoms=[Symptom(name="nausea")])
    apply_delta(profile, delta)
    assert len(profile.symptoms) == original_symptom_count


def test_apply_delta_returns_new_object():
    profile = make_profile()
    delta = empty_delta()
    result = apply_delta(profile, delta)
    assert result is not profile


def test_apply_delta_empty_delta_produces_equivalent_profile():
    profile = make_profile()
    delta = empty_delta()
    result = apply_delta(profile, delta)
    assert result.model_dump() == profile.model_dump()


# ── ProfileDelta schema validation ────────────────────────────────────────────


def test_profile_delta_rejects_invalid_symptom_type():
    with pytest.raises(Exception):
        ProfileDelta(new_symptoms="not-a-list")  # type: ignore[arg-type]


def test_profile_delta_accepts_all_empty_fields():
    delta = ProfileDelta()
    assert delta.new_symptoms == []
    assert delta.symptom_updates == []
    assert delta.free_notes_append == ""


# ── extract_profile_delta: retry integration (mocked LLM) ────────────────────

VALID_DELTA_JSON = json.dumps(
    {
        "new_symptoms": [],
        "symptom_updates": [
            {"name": "headache", "onset": "", "severity": "", "notes": "worse at night"}
        ],
        "history_additions": {
            "medical": [],
            "medications": ["ibuprofen"],
            "allergies": [],
            "family": [],
            "social": [],
        },
        "append_ruled_out": [],
        "append_ruled_in": [],
        "free_notes_append": "",
    }
)


_USAGE = LlmUsage(prompt_tokens=100, completion_tokens=None, total_tokens=None, model_actual=None)


def test_extract_profile_delta_returns_valid_delta():
    profile = make_profile()
    with patch("loop1.profile_updater.call_llm_with_usage", return_value=(VALID_DELTA_JSON, _USAGE)):
        result, usage = extract_profile_delta(profile, "Any night symptoms?", "Yes, worse at night. I take ibuprofen.")
    assert isinstance(result, ProfileDelta)
    assert result.history_additions.medications == ["ibuprofen"]
    assert usage is _USAGE


def test_extract_profile_delta_retries_on_bad_json():
    profile = make_profile()
    responses = [("}{not json", _USAGE), (VALID_DELTA_JSON, _USAGE)]
    with patch("loop1.profile_updater.call_llm_with_usage", side_effect=responses) as mock_llm:
        result, usage = extract_profile_delta(profile, "Any night symptoms?", "Yes.", max_retries=3)
    assert isinstance(result, ProfileDelta)
    assert mock_llm.call_count == 2


def test_extract_profile_delta_raises_after_max_retries():
    profile = make_profile()
    with patch("loop1.profile_updater.call_llm_with_usage", return_value=("}{bad", _USAGE)):
        with pytest.raises(ValueError, match="Failed to get a valid ProfileDelta"):
            extract_profile_delta(profile, "Q?", "A.", max_retries=3)
