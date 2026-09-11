from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from loop1.config import config
from loop1.llm import LlmUsage, call_llm_with_usage
from loop1.schemas import (
    History,
    HistoryAdditions,
    PatientProfile,
    ProfileDelta,
    Symptom,
    SymptomUpdate,
)

_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts"
_PROMPT_VERSION = config["prompt_versions"]["profile_updater"]


def _load_system_prompt() -> str:
    version = _PROMPT_VERSION.replace(".", "_")
    path = _PROMPT_DIR / f"profile_updater_{version}.txt"
    return path.read_text(encoding="utf-8").strip()


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _build_updater_prompt(
    profile: PatientProfile,
    question: str,
    answer: str,
) -> list[dict[str, str]]:
    system = _load_system_prompt()
    user = (
        "## Current patient profile\n"
        + json.dumps(profile.model_dump(), indent=2)
        + f"\n\n## Doctor's question\n{question}"
        + f"\n\n## Patient's answer\n{answer}"
        + "\n\nExtract the delta JSON now."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def extract_profile_delta(
    profile: PatientProfile,
    question: str,
    answer: str,
    max_retries: int = 3,
) -> tuple[ProfileDelta, LlmUsage]:
    messages = _build_updater_prompt(profile, question, answer)
    model = config["models"]["profile_updater"]
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
            return ProfileDelta(**data), last_usage
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
        f"Failed to get a valid ProfileDelta after {max_retries} attempts. "
        f"Last error: {last_err}"
    )


def apply_delta(profile: PatientProfile, delta: ProfileDelta) -> PatientProfile:
    """Return a new PatientProfile with delta merged in. Original is never mutated."""
    existing_names = {s.name.lower() for s in profile.symptoms}

    # Start with explicit symptom_updates from the delta
    update_map: dict[str, SymptomUpdate] = {
        u.name.lower(): u for u in delta.symptom_updates
    }

    # Safety net: if the LLM put a name-matching symptom in new_symptoms,
    # fold it into update_map as a merge rather than appending a duplicate.
    genuinely_new: list[Symptom] = []
    for sym in delta.new_symptoms:
        key = sym.name.lower()
        if key in existing_names:
            if key in update_map:
                existing_upd = update_map[key]
                merged_notes = (
                    (existing_upd.notes + "; " + sym.notes).strip("; ")
                    if sym.notes
                    else existing_upd.notes
                )
                update_map[key] = SymptomUpdate(
                    name=existing_upd.name,
                    onset=existing_upd.onset or sym.onset,
                    severity=existing_upd.severity or sym.severity,
                    notes=merged_notes,
                )
            else:
                update_map[key] = SymptomUpdate(
                    name=sym.name,
                    onset=sym.onset,
                    severity=sym.severity,
                    notes=sym.notes,
                )
        else:
            genuinely_new.append(sym)

    merged_symptoms: list[Symptom] = []
    for sym in profile.symptoms:
        upd = update_map.get(sym.name.lower())
        if upd:
            notes = sym.notes
            if upd.notes:
                notes = (notes + "; " + upd.notes).strip("; ") if notes else upd.notes
            merged_symptoms.append(
                Symptom(
                    name=sym.name,
                    onset=upd.onset or sym.onset,
                    severity=upd.severity or sym.severity,
                    notes=notes,
                )
            )
        else:
            merged_symptoms.append(sym)

    merged_symptoms.extend(genuinely_new)

    a: HistoryAdditions = delta.history_additions
    h: History = profile.history
    new_history = History(
        medical=h.medical + [x for x in a.medical if x not in h.medical],
        medications=h.medications + [x for x in a.medications if x not in h.medications],
        allergies=h.allergies + [x for x in a.allergies if x not in h.allergies],
        family=h.family + [x for x in a.family if x not in h.family],
        social=h.social + [x for x in a.social if x not in h.social],
    )

    new_ruled_out = profile.ruled_out + [
        x for x in delta.append_ruled_out if x not in profile.ruled_out
    ]
    new_ruled_in = profile.ruled_in + [
        x for x in delta.append_ruled_in if x not in profile.ruled_in
    ]

    if delta.free_notes_append:
        sep = "\n" if profile.free_notes else ""
        new_free_notes = profile.free_notes + sep + delta.free_notes_append
    else:
        new_free_notes = profile.free_notes

    return PatientProfile(
        session_id=profile.session_id,
        demographics=profile.demographics,
        chief_complaint=profile.chief_complaint,
        symptoms=merged_symptoms,
        history=new_history,
        ruled_out=new_ruled_out,
        ruled_in=new_ruled_in,
        free_notes=new_free_notes,
    )
