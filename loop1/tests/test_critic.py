from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from loop1.llm import LlmUsage
from loop2.critic.critique_schema import TurnCritique
from loop2.critic.critic import critique_turn, critique_session, _render_conversation_history

_USAGE = LlmUsage(prompt_tokens=100, completion_tokens=None, total_tokens=None, model_actual=None)

FIXTURES = Path(__file__).parent / "fixtures" / "critic_calibration"

INTEGRATION = pytest.mark.skipif(
    not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY")),
    reason="No critic API key set — skipping live critic tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_turn_event(
    turn_index: int,
    chosen_question: str,
    top_dx: str = "Pulmonary embolism",
    confidence: float = 0.45,
    rationale: str = "Test rationale.",
    patient_answer: str = "Yes.",
    secondary_dxs: list[tuple[str, float]] | None = None,
) -> dict:
    diff = [{"dx": top_dx, "prob": 0.60}]
    if secondary_dxs:
        diff += [{"dx": dx, "prob": p} for dx, p in secondary_dxs]
    return {
        "event_type": "turn_complete",
        "session_id": "test_session",
        "turn_index": turn_index,
        "doctor_output": {
            "turn_index": turn_index,
            "current_differential": diff,
            "biggest_uncertainty": "test uncertainty",
            "candidate_questions": [chosen_question],
            "chosen_question": chosen_question,
            "rationale": rationale,
            "confidence_to_stop": confidence,
            "should_stop": False,
            "safety_flags": [],
        },
        "patient_answer": patient_answer,
        "retrieved_exemplar_ids": [],
        "profile_state": {
            "session_id": "test_session",
            "demographics": {"age": 45, "sex": "M"},
            "chief_complaint": "Chest pain",
            "symptoms": [],
            "history": {"medical": [], "medications": [], "allergies": [], "family": [], "social": []},
            "ruled_out": [], "ruled_in": [], "free_notes": "", "running_summary": "",
        },
    }


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestTurnCritiqueSchema:
    def test_valid_schema(self):
        critique = TurnCritique(
            session_id="s1",
            turn=3,
            question_quality_score=0.8,
            differential_quality_score=0.7,
            reasoning_quality_score=0.9,
            confidence_calibration="well-calibrated",
            weakness=None,
            weakness_category=None,
            would_have_asked=None,
            rationale="Good turn overall.",
            critic_model="gemini/gemini-2.0-flash",
        )
        assert critique.schema_version == "0.7.0"
        assert critique.turn == 3

    def test_invalid_score_out_of_range(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            TurnCritique(
                session_id="s1",
                turn=0,
                question_quality_score=1.5,  # out of range
                differential_quality_score=0.5,
                reasoning_quality_score=0.5,
                rationale="Test.",
                critic_model="test",
            )

    def test_weakness_category_validates(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            TurnCritique(
                session_id="s1",
                turn=0,
                question_quality_score=0.5,
                differential_quality_score=0.5,
                reasoning_quality_score=0.5,
                weakness_category="invalid_category",  # not in Literal
                rationale="Test.",
                critic_model="test",
            )


class TestRenderConversationHistory:
    def test_empty_before_turn_0(self):
        events = [_make_turn_event(0, "First question?")]
        history = _render_conversation_history(events, up_to_turn=0)
        assert "(no prior turns)" in history

    def test_renders_prior_turns(self):
        events = [
            _make_turn_event(0, "Do you have chest pain?", patient_answer="Yes."),
            _make_turn_event(1, "When did it start?", patient_answer="Yesterday."),
        ]
        history = _render_conversation_history(events, up_to_turn=1)
        assert "Do you have chest pain?" in history
        assert "Yes." in history
        assert "When did it start?" not in history  # turn 1 excluded

    def test_excludes_current_turn(self):
        events = [_make_turn_event(2, "Third question?")]
        history = _render_conversation_history(events, up_to_turn=2)
        assert "Third question?" not in history


# ---------------------------------------------------------------------------
# Mock-based unit tests for critique_turn
# ---------------------------------------------------------------------------

class TestCritiqueTurnUnit:
    @patch("loop2.critic.critic._call_critic_raw")
    def test_good_turn_parses_schema(self, mock_call):
        mock_call.return_value = (json.dumps({
            "question_quality_score": 0.85,
            "differential_quality_score": 0.80,
            "reasoning_quality_score": 0.90,
            "confidence_calibration": "well-calibrated",
            "weakness": None,
            "weakness_category": None,
            "would_have_asked": None,
            "rationale": "Strong question targeting the main uncertainty.",
        }), _USAGE)
        event = _make_turn_event(0, "Do you have leg swelling?")
        critique, usage = critique_turn(event, [event], "test_sess")
        assert isinstance(critique, TurnCritique)
        assert critique.question_quality_score == pytest.approx(0.85)
        assert critique.turn == 0
        assert usage is _USAGE

    @patch("loop2.critic.critic._call_critic_raw")
    def test_bad_json_retries(self, mock_call):
        mock_call.side_effect = [
            ("not json at all ```", _USAGE),
            (json.dumps({
                "question_quality_score": 0.5,
                "differential_quality_score": 0.5,
                "reasoning_quality_score": 0.5,
                "confidence_calibration": None,
                "weakness": None,
                "weakness_category": None,
                "would_have_asked": None,
                "rationale": "Retry worked.",
            }), _USAGE),
        ]
        event = _make_turn_event(0, "Test question?")
        critique, usage = critique_turn(event, [event], "sess")
        assert critique.rationale == "Retry worked."

    @patch("loop2.critic.critic._call_critic_raw")
    def test_all_retries_fail_raises(self, mock_call):
        mock_call.return_value = ("definitely not json {", _USAGE)
        event = _make_turn_event(0, "Test?")
        with pytest.raises(ValueError, match="Failed to get a valid TurnCritique"):
            critique_turn(event, [event], "sess")


# ---------------------------------------------------------------------------
# Calibration test — must pass 3/3 before running on DDXPlus patients
# ---------------------------------------------------------------------------

@INTEGRATION
class TestCriticCalibration:
    """
    Run the critic on the Phase 6 demo session (case_01) and verify it catches
    all 3 known failure modes. This test is the gate before the integrated runner.

    Pass threshold: 3/3. If 2/3, iterate the critic prompt before proceeding.
    """

    @classmethod
    def _load_session_events(cls) -> list[dict]:
        path = FIXTURES / "case_01_session.jsonl"
        events = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    @classmethod
    def _critique_all_turns(cls) -> list[TurnCritique]:
        events = cls._load_session_events()
        return critique_session(events, session_id="d7f3a9b2-6c8e-4e93-b4a0-1a4f8f0f2101")

    def test_failure_1_poor_differential_caught(self):
        """
        Failure 1: ACS ranked above PE despite Mexico trip + leg swelling + confirmed DVT.
        Expect at least one turn where differential_quality_score < 0.5
        AND weakness_category == 'poor_differential'.
        """
        critiques = self._critique_all_turns()
        flagged = [
            c for c in critiques
            if c.differential_quality_score < 0.5
            and c.weakness_category == "poor_differential"
        ]
        assert flagged, (
            "Failure mode 1 NOT caught: no turn has differential_quality_score < 0.5 "
            "with weakness_category='poor_differential'.\n"
            "Critic differential scores: "
            + str([(c.turn, c.differential_quality_score, c.weakness_category) for c in critiques])
        )

    def test_failure_2_missed_red_flag_caught(self):
        """
        Failure 2: Turn 7 asked hemoptysis instead of dyspnea/pleurisy.
        Expect turn 7 (or adjacent) has question_quality_score < 0.5,
        weakness_category == 'missed_red_flag', and would_have_asked mentions dyspnea or pleurisy.
        """
        critiques = self._critique_all_turns()
        flagged = [
            c for c in critiques
            if c.question_quality_score < 0.5
            and c.weakness_category == "missed_red_flag"
            and c.would_have_asked is not None
            and any(
                kw in c.would_have_asked.lower()
                for kw in ("dyspnea", "breath", "pleuris", "pleuritic")
            )
        ]
        assert flagged, (
            "Failure mode 2 NOT caught: no turn has question_quality_score < 0.5 "
            "with weakness_category='missed_red_flag' mentioning dyspnea/pleurisy.\n"
            "Critic question scores: "
            + str([(c.turn, c.question_quality_score, c.weakness_category, c.would_have_asked) for c in critiques])
        )

    def test_failure_3_redundant_question_caught(self):
        """
        Failure 3: Chest pain character asked at turns 0, 4, and 8.
        Expect at least one of turns 4 or 8 has weakness_category == 'redundant_question'.
        """
        critiques = self._critique_all_turns()
        redundant_at_4_or_8 = [
            c for c in critiques
            if c.turn in (4, 8)
            and c.weakness_category == "redundant_question"
        ]
        assert redundant_at_4_or_8, (
            "Failure mode 3 NOT caught: turns 4 and 8 do not have weakness_category='redundant_question'.\n"
            "Critic categories at turns 4/8: "
            + str([(c.turn, c.weakness_category, c.question_quality_score) for c in critiques if c.turn in (4, 8)])
        )

    def test_all_three_failure_modes_caught(self):
        """
        Meta-test: all 3/3 failure modes caught. This is the gate before Step 7.
        """
        critiques = self._critique_all_turns()

        # Failure 1
        fm1 = any(
            c.differential_quality_score < 0.5 and c.weakness_category == "poor_differential"
            for c in critiques
        )
        # Failure 2
        fm2 = any(
            c.question_quality_score < 0.5
            and c.weakness_category == "missed_red_flag"
            and c.would_have_asked is not None
            and any(kw in c.would_have_asked.lower() for kw in ("dyspnea", "breath", "pleuris", "pleuritic"))
            for c in critiques
        )
        # Failure 3
        fm3 = any(
            c.turn in (4, 8) and c.weakness_category == "redundant_question"
            for c in critiques
        )

        score = sum([fm1, fm2, fm3])
        assert score == 3, (
            f"Calibration: {score}/3 failure modes caught. "
            f"fm1={fm1}, fm2={fm2}, fm3={fm3}.\n"
            "Critic output:\n"
            + "\n".join(
                f"  turn {c.turn}: q={c.question_quality_score:.2f} diff={c.differential_quality_score:.2f} "
                f"cat={c.weakness_category} would_have_asked={c.would_have_asked}"
                for c in critiques
            )
        )
