from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from loop1.config import config
from loop1.llm import LlmUsage, call_llm_with_usage
from loop1.retrieval import get_exemplar_by_id, get_exemplars, get_exemplars_for_profile
from loop1.schemas import DoctorTurnOutput, Exemplar, PatientProfile, TurnRecord

_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts"
_PROMPT_VERSION = config["prompt_versions"]["doctor"]


def _load_system_prompt() -> str:
    version = _PROMPT_VERSION.replace(".", "_")
    path = _PROMPT_DIR / f"doctor_{version}.txt"
    return path.read_text(encoding="utf-8").strip()


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _render_exemplars(exemplars: list[Exemplar]) -> str:
    lines: list[str] = [
        "## Exemplar demonstrations\n",
        "Apply the reasoning *pattern* from each example — do not copy or paraphrase the literal question.\n",
    ]
    for ex in exemplars:
        lines.append("---")
        lines.append(f"**Situation:** {ex.context_summary}")
        lines.append(f"**Question chosen:** \"{ex.good_next_question}\"")
        lines.append(f"**Why it was strong:** {ex.rationale}")
        lines.append("")
    lines.append("---\n")
    return "\n".join(lines)


def build_doctor_prompt(
    profile: PatientProfile,
    history: list[TurnRecord],
    turn_index: int,
    exemplars: list[Exemplar] | None = None,
) -> list[dict[str, str]]:
    system = _load_system_prompt()

    parts: list[str] = []

    if exemplars:
        parts.append(_render_exemplars(exemplars))

    parts.append("## Patient profile\n")
    parts.append(json.dumps(profile.model_dump(), indent=2))

    if history:
        parts.append("\n## Conversation history")
        for turn in history:
            diff_str = ", ".join(
                f"{e.dx}={e.prob:.2f}"
                for e in turn.doctor_output.current_differential[:4]
            )
            parts.append(f"\nTurn {turn.turn_index}:")
            parts.append(f"  Prior differential: {diff_str}")
            parts.append(f"  Doctor asked      : {turn.doctor_output.chosen_question}")
            parts.append(f"  Patient answer    : {turn.patient_answer}")

    parts.append(f"\nProduce the output JSON for turn_index {turn_index}.")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(parts)},
    ]


def generate_turn_with_usage(
    profile: PatientProfile,
    history: list[TurnRecord] | None = None,
    turn_index: int = 0,
    max_retries: int = 3,
    rng: random.Random | None = None,
    forced_exemplars: list[Exemplar] | None = None,
) -> tuple[DoctorTurnOutput, LlmUsage, list[str]]:
    """
    Returns (DoctorTurnOutput, LlmUsage, exemplar_ids).
    LlmUsage is from the final successful API call.
    exemplar_ids are the IDs of exemplars injected into this turn's prompt.
    Pass a seeded rng for reproducible exemplar selection.
    Pass forced_exemplars to bypass retrieval entirely (useful for ablation runs).
    """
    if forced_exemplars is not None:
        exemplars = forced_exemplars
    else:
        exemplars = get_exemplars_for_profile(profile, n=config["thresholds"]["top_k_exemplars"])
    exemplar_ids = [ex.exemplar_id for ex in exemplars]

    messages = build_doctor_prompt(profile, history or [], turn_index, exemplars)
    model = config["models"]["doctor"]
    last_err: Exception | None = None
    last_usage: LlmUsage | None = None

    for attempt in range(max_retries):
        raw, usage = call_llm_with_usage(model=model, messages=messages)
        last_usage = usage
        cleaned = _strip_fences(raw)

        try:
            data: dict[str, Any] = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            last_err = exc
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"Your response was not valid JSON. "
                        f"Parse error on attempt {attempt + 1}: {exc}. "
                        "Reply with valid JSON only — no markdown fences, no prose."
                    ),
                },
            ]
            continue

        try:
            return DoctorTurnOutput(**data), last_usage, exemplar_ids
        except ValidationError as exc:
            last_err = exc
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"Your JSON parsed but failed schema validation on attempt {attempt + 1}. "
                        f"Errors: {exc}. "
                        "Fix the issues and reply with the corrected JSON only."
                    ),
                },
            ]

    raise ValueError(
        f"Failed to get a valid DoctorTurnOutput after {max_retries} attempts. "
        f"Last error: {last_err}"
    )


def generate_turn(
    profile: PatientProfile,
    history: list[TurnRecord] | None = None,
    turn_index: int = 0,
    max_retries: int = 3,
    rng: random.Random | None = None,
) -> DoctorTurnOutput:
    output, _usage, _exemplar_ids = generate_turn_with_usage(
        profile, history, turn_index, max_retries, rng=rng
    )
    return output
