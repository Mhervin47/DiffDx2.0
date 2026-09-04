"""week1.md Task 6 — Redis session state. Covers APISession.to_dict()/
from_dict() round-tripping and session_store's get/save/delete against
both backends: the in-memory fallback (no REDIS_URL) and a real
redis-py-shaped client via fakeredis (no live Redis server needed/available
in this environment — see TASK plan for why this is the honest limit of
what's tested here)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from diffdx import session_store
from loop1.schemas import (
    Demographics,
    DiagnosisEntry,
    DoctorTurnOutput,
    History,
    PatientProfile,
    Symptom,
    TurnRecord,
)
from web.api_session import APISession


def _profile(**kwargs) -> PatientProfile:
    defaults = dict(
        session_id="test-session-id",
        demographics=Demographics(age=35, sex="F"),
        chief_complaint="headache",
        symptoms=[Symptom(name="headache", onset="2 days", severity="moderate", notes="")],
        history=History(),
    )
    defaults.update(kwargs)
    return PatientProfile(**defaults)


def _doctor_output(**kwargs) -> DoctorTurnOutput:
    defaults = dict(
        turn_index=0,
        current_differential=[DiagnosisEntry(dx="Migraine", prob=0.6)],
        biggest_uncertainty="Duration",
        candidate_questions=["How long does it last?"],
        chosen_question="How long does it last?",
        rationale="Narrowing differential",
        confidence_to_stop=0.4,
        should_stop=False,
    )
    defaults.update(kwargs)
    return DoctorTurnOutput(**defaults)


def _session_with_history() -> APISession:
    session = APISession(_profile(), max_turns=6)
    session.history.append(
        TurnRecord(
            turn_index=0,
            doctor_output=_doctor_output(),
            patient_answer="About 3 hours",
            retrieved_exemplar_ids=["ex1"],
            timestamp="2026-01-01T00:00:00+00:00",
        )
    )
    session._pending_turn_index = 1
    session._pending_doctor_output = _doctor_output(turn_index=1)
    session._pending_prompt_tokens = 42
    session._pending_exemplar_ids = ["ex1", "ex2"]
    session.session_language = "hi-IN"
    return session


# ---------------------------------------------------------------------------
# APISession.to_dict() / from_dict()
# ---------------------------------------------------------------------------

def test_to_dict_from_dict_round_trip_preserves_state():
    original = _session_with_history()
    restored = APISession.from_dict(original.to_dict())

    assert restored.session_id == original.session_id
    assert restored.max_turns == original.max_turns
    assert restored.confidence_threshold == original.confidence_threshold
    assert restored.keep_recent == original.keep_recent
    assert len(restored.history) == 1
    assert restored.history[0].patient_answer == "About 3 hours"
    assert restored.history[0].doctor_output.chosen_question == "How long does it last?"
    assert restored._pending_turn_index == 1
    assert restored._pending_doctor_output.turn_index == 1
    assert restored._pending_prompt_tokens == 42
    assert restored._pending_exemplar_ids == ["ex1", "ex2"]
    assert restored.session_language == "hi-IN"
    assert restored.complete is False
    assert restored._final_record is None
    # _on_change is never serialized — always None after from_dict, the
    # caller re-attaches it.
    assert restored._on_change is None


def test_to_dict_from_dict_round_trip_preserves_completed_session():
    session = _session_with_history()
    session.complete = True
    session.termination_reason = "max_turns"
    doctor_output = _doctor_output(current_differential=[DiagnosisEntry(dx="Tension Headache", prob=0.8)])
    session._finalize("max_turns", doctor_output)

    restored = APISession.from_dict(session.to_dict())
    assert restored.complete is True
    assert restored.termination_reason == "max_turns"
    assert restored._final_record is not None
    assert restored._final_record.primary_diagnosis == "Tension Headache"
    assert restored._final_record.session_id == session.session_id


def test_from_dict_defaults_session_language_when_absent():
    """Backward compat: a payload serialized before session_language was
    declared in __init__ (or missing for any other reason) still restores."""
    data = _session_with_history().to_dict()
    del data["session_language"]
    restored = APISession.from_dict(data)
    assert restored.session_language == "en-IN"


# ---------------------------------------------------------------------------
# session_store — in-memory fallback (no REDIS_URL)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_in_memory_store():
    session_store._sessions.clear()
    yield
    session_store._sessions.clear()


def test_in_memory_get_save_delete_round_trip(monkeypatch):
    monkeypatch.setattr(session_store, "_USE_REDIS", False)
    session = _session_with_history()

    assert session_store.get_session(session.session_id) is None
    session_store.save_session(session.session_id, session)
    got = session_store.get_session(session.session_id)
    assert got is session  # in-memory fallback holds the live reference

    session_store.delete_session(session.session_id)
    assert session_store.get_session(session.session_id) is None


# ---------------------------------------------------------------------------
# session_store — Redis-shaped path via fakeredis (no live server needed)
# ---------------------------------------------------------------------------

@pytest.fixture
def _fake_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(session_store, "_USE_REDIS", True)
    monkeypatch.setattr(session_store, "_get_redis", lambda: client)
    return client


def test_redis_get_save_delete_round_trip(_fake_redis):
    session = _session_with_history()

    assert session_store.get_session(session.session_id) is None
    session_store.save_session(session.session_id, session)

    got = session_store.get_session(session.session_id)
    assert got is not session  # real round trip through JSON, not a shared reference
    assert got.session_id == session.session_id
    assert got.history[0].patient_answer == "About 3 hours"
    assert got.session_language == "hi-IN"

    session_store.delete_session(session.session_id)
    assert session_store.get_session(session.session_id) is None


def test_redis_save_sets_ttl(_fake_redis):
    session = _session_with_history()
    session_store.save_session(session.session_id, session, ttl_seconds=100)
    ttl = _fake_redis.ttl(session_store._redis_key(session.session_id))
    assert 0 < ttl <= 100


def test_redis_error_maps_to_503(monkeypatch):
    import redis

    class _BrokenRedis:
        def get(self, key):
            raise redis.RedisError("connection refused")

        def set(self, key, payload, ex=None):
            raise redis.RedisError("connection refused")

        def delete(self, key):
            raise redis.RedisError("connection refused")

    monkeypatch.setattr(session_store, "_USE_REDIS", True)
    monkeypatch.setattr(session_store, "_get_redis", lambda: _BrokenRedis())

    with pytest.raises(HTTPException) as exc_info:
        session_store.get_session("some-id")
    assert exc_info.value.status_code == 503

    with pytest.raises(HTTPException) as exc_info:
        session_store.save_session("some-id", _session_with_history())
    assert exc_info.value.status_code == 503

    with pytest.raises(HTTPException) as exc_info:
        session_store.delete_session("some-id")
    assert exc_info.value.status_code == 503
