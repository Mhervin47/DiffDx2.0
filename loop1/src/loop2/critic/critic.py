from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from loop1.llm import call_llm
from loop2.critic.critique_schema import TurnCritique

_log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "critic_turn_v0_1.txt"

_DEFAULT_CRITIC_MODEL = "groq/llama-3.3-70b-versatile"
_MAX_RETRIES = 3


def _critic_model() -> str:
    return os.environ.get("CRITIC_MODEL", _DEFAULT_CRITIC_MODEL)


def _load_prompt_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


def _extract_json(text: str) -> str:
    """
    Extract the first complete JSON object from text.
    Handles markdown fences, thinking preamble, and trailing prose.
    """
    text = text.strip()
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    # Find the first '{' and the matching '}'
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


def _render_conversation_history(session_events: list[dict], up_to_turn: int) -> str:
    """Render conversation turns strictly before `up_to_turn`."""
    lines: list[str] = []
    for event in session_events:
        if event.get("event_type") != "turn_complete":
            continue
        turn_index = event.get("turn_index", event.get("doctor_output", {}).get("turn_index", -1))
        if turn_index >= up_to_turn:
            continue
        doc = event.get("doctor_output", {})
        answer = event.get("patient_answer", "")
        lines.append(f"Turn {turn_index}:")
        lines.append(f"  Doctor asked: {doc.get('chosen_question', '')}")
        lines.append(f"  Patient answered: {answer}")
    return "\n".join(lines) if lines else "(no prior turns)"


def _call_critic_raw(messages: list[dict]) -> str:
    """Call the critic model using the same httpx path as the doctor LLM."""
    model = _critic_model()
    return call_llm(model, messages, temperature=0, max_tokens=1024)


def critique_turn(
    turn_event: dict[str, Any],
    session_events: list[dict[str, Any]],
    session_id: str,
) -> TurnCritique:
    """
    Critique a single doctor turn. One LLM call per turn.

    Args:
        turn_event: The turn_complete event dict for the turn to critique.
        session_events: All events in the session (used to build conversation history).
        session_id: Session identifier for the output record.

    Returns:
        TurnCritique with all scores and metadata populated.
    """
    doc = turn_event.get("doctor_output", {})
    # Use the log event's turn_index — it's set by the session loop and is always correct.
    # doctor_output.turn_index is LLM-generated and can be wrong (especially with smaller models).
    turn_index: int = turn_event.get("turn_index", doc.get("turn_index", 0))
    profile_state = turn_event.get("profile_state", {})

    template = _load_prompt_template()
    history_text = _render_conversation_history(session_events, up_to_turn=turn_index)

    prompt = (
        template
        .replace("{conversation_history}", history_text)
        .replace("{patient_profile_json}", json.dumps(profile_state, indent=2))
        .replace("{turn_index}", str(turn_index))
        .replace("{chosen_question}", doc.get("chosen_question", ""))
        .replace("{doctor_rationale}", doc.get("rationale", ""))
        .replace("{differential_json}", json.dumps(doc.get("current_differential", []), indent=2))
        .replace("{confidence_to_stop}", str(doc.get("confidence_to_stop", 0.0)))
        .replace("{biggest_uncertainty}", doc.get("biggest_uncertainty", ""))
    )

    messages = [{"role": "user", "content": prompt}]
    model = _critic_model()
    last_err: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        try:
            raw = _call_critic_raw(messages)
        except Exception as exc:
            last_err = exc
            _log.warning("Critic API call failed on attempt %d: %s", attempt + 1, exc)
            continue

        cleaned = _extract_json(raw)
        try:
            data: dict[str, Any] = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            last_err = exc
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"Your response was not valid JSON. Error: {exc}. "
                        "Reply with valid JSON only — no markdown fences, no prose."
                    ),
                },
            ]
            continue

        try:
            return TurnCritique(
                session_id=session_id,
                turn=turn_index,
                critic_model=model,
                **data,
            )
        except ValidationError as exc:
            last_err = exc
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"Your JSON failed schema validation. Errors: {exc}. "
                        "Fix and reply with corrected JSON only."
                    ),
                },
            ]

    raise ValueError(
        f"Failed to get a valid TurnCritique after {_MAX_RETRIES} attempts. "
        f"Last error: {last_err}"
    )


def critique_session(
    session_events: list[dict[str, Any]],
    session_id: str,
) -> list[TurnCritique]:
    """Critique all turn_complete events in a session. Returns one TurnCritique per turn."""
    critiques: list[TurnCritique] = []
    turn_events = [e for e in session_events if e.get("event_type") == "turn_complete"]

    for event in turn_events:
        critique = critique_turn(event, session_events, session_id)
        critiques.append(critique)
        _log.info(
            "Critiqued turn %d: q=%.2f diff=%.2f cat=%s",
            critique.turn,
            critique.question_quality_score,
            critique.differential_quality_score,
            critique.weakness_category,
        )

    return critiques
