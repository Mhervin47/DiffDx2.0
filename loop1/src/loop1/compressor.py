from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from loop1.config import config
from loop1.llm import LlmUsage, call_llm_with_usage
from loop1.schemas import History, PatientProfile, Symptom, TurnRecord

_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts"
_PROMPT_VERSION = config["prompt_versions"]["compressor"]


def _load_system_prompt() -> str:
    version = _PROMPT_VERSION.replace(".", "_")
    path = _PROMPT_DIR / f"compressor_{version}.txt"
    return path.read_text(encoding="utf-8").strip()


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Code-level deduplication (pure Python, no LLM)
# ---------------------------------------------------------------------------

def _dedup_notes(notes: str) -> str:
    """Remove duplicate phrases within a semicolon-separated notes string."""
    if not notes:
        return notes
    parts = [p.strip() for p in notes.split(";") if p.strip()]
    seen: set[str] = set()
    unique: list[str] = []
    for p in parts:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return "; ".join(unique)


def _dedup_sentences(text: str) -> str:
    """Remove duplicate sentences in a free-text block (newline or period-separated)."""
    if not text:
        return text
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    seen: set[str] = set()
    unique: list[str] = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        key = s.lower().rstrip(".!?")
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return " ".join(unique)


def _dedup_list(items: list[str]) -> list[str]:
    """Deduplicate a list of strings case-insensitively, preserving original case of first occurrence."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower().strip()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


_STOP_WORDS = frozenset({
    "a", "an", "the", "in", "of", "at", "on", "to", "for", "with",
    "and", "or", "episode", "episodes",
})


def _content_words(name: str) -> frozenset[str]:
    """Extract meaningful words: lowercase, remove stop words, naive stem (strip trailing 's')."""
    words = re.sub(r"[^a-z\s]", "", name.lower()).split()
    result: set[str] = set()
    for w in words:
        if w in _STOP_WORDS:
            continue
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        result.add(w)
    return frozenset(result)


def _extract_qualifier(specific: str, canonical: str) -> str:
    """
    If canonical is a prefix of specific, return the trailing qualifier.
    E.g. specific="chest pain at rest", canonical="chest pain" → "at rest".
    Returns "" when no clean prefix extraction is possible.
    """
    if specific.lower().startswith(canonical.lower()):
        return specific[len(canonical):].strip()
    return ""


def _merge_pair(canonical: Symptom, absorbed: Symptom, qualifier: str = "") -> Symptom:
    """Merge absorbed into canonical, carrying over onset/severity/notes and qualifier."""
    extra = "; ".join(filter(None, [qualifier, absorbed.notes]))
    combined_notes = _dedup_notes(
        "; ".join(filter(None, [canonical.notes, extra]))
    )
    return Symptom(
        name=canonical.name,
        onset=canonical.onset or absorbed.onset,
        severity=canonical.severity or absorbed.severity,
        notes=combined_notes,
    )


def _find_canonical(a_name: str, b_name: str) -> tuple[bool, bool]:
    """
    Decide whether two symptom names refer to the same symptom.
    Returns (should_merge, b_is_canonical).

    Checks in order:
    1. Substring containment: if a_lower in b_lower → a is simpler (b_is_canonical=False).
    2. Substring containment: if b_lower in a_lower → b is simpler (b_is_canonical=True).
    3. Content-word subset: if content_words(a) ⊆ content_words(b) → a is simpler.
    4. Content-word subset: if content_words(b) ⊆ content_words(a) → b is simpler.
    """
    al = a_name.lower()
    bl = b_name.lower()

    if al in bl:
        return True, False
    if bl in al:
        return True, True

    ac = _content_words(a_name)
    bc = _content_words(b_name)
    if ac and bc:
        if ac <= bc:
            return True, False
        if bc <= ac:
            return True, True

    return False, False


def _similarity_ratio(a_words: frozenset[str], b_words: frozenset[str]) -> float:
    """Jaccard-style ratio: |intersection| / |union| of content word sets."""
    if not a_words or not b_words:
        return 0.0
    return len(a_words & b_words) / len(a_words | b_words)


def _dedup_symptoms(symptoms: list[Symptom]) -> list[Symptom]:
    """
    Four-pass deduplication:
    1. Merge exact name matches (case-insensitive).
    2. Merge substring containment: "chest pain" absorbs "left-sided chest pain".
    3. Merge content-word subset: "swelling in legs" absorbs "left leg swelling"
       because {"swelling","leg"} ⊆ {"left","leg","swelling"}.
    4. Merge high-similarity variants (>80% Jaccard on content words): keeps the
       longer/more specific entry and discards the shorter variant.
    In all cases the simpler/shorter name is kept for passes 1-3; qualifiers move to notes.
    """
    # Pass 1: merge exact (case-insensitive) name matches
    by_key: dict[str, Symptom] = {}
    for sym in symptoms:
        key = sym.name.lower().strip()
        if key in by_key:
            existing = by_key[key]
            merged_notes = "; ".join(filter(None, [existing.notes, sym.notes]))
            by_key[key] = Symptom(
                name=existing.name,
                onset=existing.onset or sym.onset,
                severity=existing.severity or sym.severity,
                notes=_dedup_notes(merged_notes),
            )
        else:
            by_key[key] = Symptom(
                name=sym.name,
                onset=sym.onset,
                severity=sym.severity,
                notes=_dedup_notes(sym.notes),
            )

    # Pass 2 & 3: merge via substring containment then content-word subset
    items = list(by_key.values())
    absorbed_set: set[int] = set()
    result: list[Symptom] = []

    for i, base in enumerate(items):
        if i in absorbed_set:
            continue
        merged = base
        for j, other in enumerate(items):
            if j <= i or j in absorbed_set:
                continue
            should_merge, other_is_canonical = _find_canonical(merged.name, other.name)
            if not should_merge:
                continue
            if other_is_canonical:
                # other is the simpler name; fold merged into other
                qualifier = _extract_qualifier(merged.name, other.name)
                merged = _merge_pair(other, merged, qualifier)
            else:
                # merged is the simpler name; fold other into merged
                qualifier = _extract_qualifier(other.name, merged.name)
                merged = _merge_pair(merged, other, qualifier)
            absorbed_set.add(j)
        result.append(merged)

    # Pass 4: merge high-similarity variants (>80% Jaccard on content words)
    # Keep the longer (more specific) entry.
    absorbed4: set[int] = set()
    final: list[Symptom] = []
    for i, sym_i in enumerate(result):
        if i in absorbed4:
            continue
        current = sym_i
        for j, sym_j in enumerate(result):
            if j <= i or j in absorbed4:
                continue
            wi = _content_words(current.name)
            wj = _content_words(sym_j.name)
            if _similarity_ratio(wi, wj) > 0.80:
                # Keep the longer name (more specific); merge notes
                if len(sym_j.name) > len(current.name):
                    merged_notes = _dedup_notes(
                        "; ".join(filter(None, [sym_j.notes, current.notes]))
                    )
                    current = Symptom(
                        name=sym_j.name,
                        onset=sym_j.onset or current.onset,
                        severity=sym_j.severity or current.severity,
                        notes=merged_notes,
                    )
                else:
                    merged_notes = _dedup_notes(
                        "; ".join(filter(None, [current.notes, sym_j.notes]))
                    )
                    current = Symptom(
                        name=current.name,
                        onset=current.onset or sym_j.onset,
                        severity=current.severity or sym_j.severity,
                        notes=merged_notes,
                    )
                absorbed4.add(j)
        final.append(current)

    return final


def _dedup_profile(profile: PatientProfile) -> PatientProfile:
    """Return a new PatientProfile with all code-level duplicates removed."""
    return PatientProfile(
        session_id=profile.session_id,
        demographics=profile.demographics,
        chief_complaint=profile.chief_complaint,
        symptoms=_dedup_symptoms(profile.symptoms),
        history=History(
            medical=_dedup_list(profile.history.medical),
            medications=_dedup_list(profile.history.medications),
            allergies=_dedup_list(profile.history.allergies),
            family=_dedup_list(profile.history.family),
            social=_dedup_list(profile.history.social),
        ),
        ruled_out=_dedup_list(profile.ruled_out),
        ruled_in=_dedup_list(profile.ruled_in),
        free_notes=_dedup_sentences(profile.free_notes),
        running_summary=profile.running_summary,
    )


# ---------------------------------------------------------------------------
# LLM-based recategorization + summarization
# ---------------------------------------------------------------------------

def _build_compressor_prompt(
    profile: PatientProfile,
    turns_to_summarize: list[TurnRecord],
) -> list[dict[str, str]]:
    system = _load_system_prompt()

    parts: list[str] = ["## Current patient profile (history section)\n"]
    parts.append(json.dumps(profile.history.model_dump(), indent=2))

    parts.append("\n\n## Dialogue turns to summarize")
    for turn in turns_to_summarize:
        parts.append(f"\nTurn {turn.turn_index}:")
        parts.append(f"  Doctor asked  : {turn.doctor_output.chosen_question}")
        parts.append(f"  Patient answer: {turn.patient_answer}")

    parts.append("\n\nProduce the corrected_history and running_summary JSON now.")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(parts)},
    ]


def _llm_recategorize_and_summarize(
    profile: PatientProfile,
    turns_to_summarize: list[TurnRecord],
    max_retries: int = 3,
) -> tuple[History, str, LlmUsage]:
    messages = _build_compressor_prompt(profile, turns_to_summarize)
    model = config["models"]["compressor"]
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
            corrected_h = History(**data["corrected_history"])
            running_summary = str(data.get("running_summary", "")).strip()
            return corrected_h, running_summary, last_usage
        except (KeyError, ValidationError, TypeError) as exc:
            last_err = exc
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"Your JSON was invalid on attempt {attempt + 1}. "
                        f"Error: {exc}. "
                        "Return JSON with keys 'corrected_history' (object with medical, "
                        "medications, allergies, family, social arrays) and 'running_summary' (string)."
                    ),
                },
            ]

    raise ValueError(
        f"Compressor failed after {max_retries} attempts. Last error: {last_err}"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compress_context(
    profile: PatientProfile,
    history: list[TurnRecord],
    keep_recent: int | None = None,
) -> tuple[PatientProfile, LlmUsage | None]:
    """
    Compress context by:
    1. Code-level dedup of symptoms, history lists, free_notes.
    2. If there are turns older than keep_recent, call the LLM to:
       - Fix history mis-categorization.
       - Generate a running_summary of the compressed turns.

    Returns (PatientProfile, LlmUsage | None) — a new PatientProfile with
    deduped fields and updated running_summary, plus the LLM call's usage
    (None when no turns needed summarizing, since no call was made). The
    caller is responsible for passing only history[-keep_recent:] to the
    doctor on subsequent turns.
    """
    if keep_recent is None:
        keep_recent = config["thresholds"]["compression_keep_recent"]

    cleaned = _dedup_profile(profile)

    turns_to_summarize = history[:-keep_recent] if len(history) > keep_recent else []

    if not turns_to_summarize:
        return cleaned, None

    corrected_history, running_summary, usage = _llm_recategorize_and_summarize(
        cleaned, turns_to_summarize
    )

    return PatientProfile(
        session_id=cleaned.session_id,
        demographics=cleaned.demographics,
        chief_complaint=cleaned.chief_complaint,
        symptoms=cleaned.symptoms,
        history=corrected_history,
        ruled_out=cleaned.ruled_out,
        ruled_in=cleaned.ruled_in,
        free_notes=cleaned.free_notes,
        running_summary=running_summary,
    ), usage
