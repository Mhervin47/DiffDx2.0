"""
Single-shot "traditional LLM" baseline harness — admin portal, Phase 1.

Runs the same DDXPlus eval-set patients through one plain LLM call each (no
multi-turn interview, no critic, no exemplar retrieval) and writes session
JSONL in the exact shape loop2.ddxplus.evaluator.evaluate_session() expects,
so that function is reused completely unchanged.

Two modes:
  a — matched-interaction (primary/headline number). The baseline sees only
      the patient's opening complaint (one SimulatedPatient.initial_complaint()
      call) — the same information the actor has at turn 0.
  b — matched-information (secondary/ablation). The baseline sees the full
      structured symptoms/antecedents record directly.

Usage:
    python src/admin_portal/eval/baseline_eval.py [--mode {a,b,both}]
        [--max-patients N] [--model MODEL] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

# src/admin_portal/eval/baseline_eval.py -> eval -> admin_portal -> src -> loop1
# 4 parent hops — verified for this exact file location (see build brief
# Section 9.1: this is the specific off-by-one this pattern is prone to).
_LOOP1_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_LOOP1_ROOT / "src"))

_EVAL_SET_PATH = _LOOP1_ROOT / "data" / "ddxplus_eval_set.json"
_DATA_DIR = _LOOP1_ROOT / "data" / "admin_portal"
_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

_SESSION_SCHEMA_VERSION = "0.5.0"  # matches loop1/config.yaml's logging.schema_version
_MAX_ATTEMPTS = 3  # 1 initial call + 2 reformat retries, per the brief
_JSON_RETRY_NUDGE = (
    "Your previous response was not valid JSON matching the required shape. "
    "Reply again with ONLY the corrected JSON object — no markdown fences, no "
    "prose, no additional reasoning beyond what's needed to fill the fields."
)
_LIST_ITEM_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*(.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Eval set / patient helpers
# ---------------------------------------------------------------------------

def _load_eval_set() -> list[dict]:
    if not _EVAL_SET_PATH.exists():
        raise SystemExit(
            f"Eval set not found at {_EVAL_SET_PATH}.\n"
            "Run `python scripts/curate_eval_set.py` from inside loop1/ first "
            "to generate it, then re-run this script."
        )
    with open(_EVAL_SET_PATH, encoding="utf-8") as f:
        return json.load(f)


def _build_patient(entry: dict):
    from loop2.ddxplus.schemas import DDXPlusPatient

    return DDXPlusPatient(
        patient_id=entry["patient_id"],
        age=entry["age"],
        sex=entry["sex"],
        initial_evidence=entry["initial_evidence"],
        initial_evidence_code=entry["initial_evidence_code"],
        symptoms=entry["symptoms"],
        antecedents=entry["antecedents"],
        ground_truth_pathology=entry["ground_truth_pathology"],
        ground_truth_differential=[
            tuple(pair) for pair in entry["ground_truth_differential"]
        ],
    )


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def _load_prompt_template(mode: str) -> str:
    path = _PROMPT_DIR / f"baseline_mode_{mode}_v0_1.txt"
    return path.read_text(encoding="utf-8")


def _render_prompt_mode_a(entry: dict, chief_complaint: str) -> str:
    template = _load_prompt_template("a")
    return template.format(
        age=entry["age"], sex=entry["sex"], chief_complaint=chief_complaint
    )


def _render_prompt_mode_b(entry: dict) -> str:
    template = _load_prompt_template("b")
    return template.format(
        age=entry["age"],
        sex=entry["sex"],
        symptoms_json=json.dumps(entry["symptoms"], indent=2),
        antecedents_json=json.dumps(entry["antecedents"], indent=2),
    )


# ---------------------------------------------------------------------------
# JSON parsing, validation, and the intentional failure ladder
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _extract_and_validate(raw: str) -> dict | None:
    """Parse raw text as the expected baseline JSON shape, or return None."""
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    diff = data.get("differential")
    if not isinstance(diff, list) or not diff:
        return None
    for d in diff:
        if not isinstance(d, dict) or "dx" not in d or "prob" not in d:
            return None
    return data


def _regex_fallback_differential(raw_text: str) -> list[dict]:
    """
    Weak fallback: pull a numbered/bulleted list of candidate diagnoses out of
    unstructured text. Deliberately naive — this path exists to surface a real
    parse failure honestly (via parsed_ok=False), not to rescue bad output
    into looking clean.
    """
    matches = [m.strip(" .:-•") for m in _LIST_ITEM_RE.findall(raw_text)]
    candidates = [m for m in matches if m]
    if not candidates:
        return []
    n = len(candidates)
    weight_total = sum(range(1, n + 1))
    return [
        {"dx": name[:120], "prob": round((n - i) / weight_total, 4)}
        for i, name in enumerate(candidates)
    ]


def _run_one_call(model: str, prompt: str) -> tuple[dict, int, float, bool]:
    """
    Call the model with up to _MAX_ATTEMPTS tries, retrying only on invalid
    JSON (a formatting nudge, not extra reasoning). Returns:
      (parsed_output, total_prompt_tokens, total_latency_ms, parsed_ok)
    parsed_output always has 'differential' (list of {dx, prob}), 'confidence',
    and 'reasoning' keys — falling back to a weak regex extraction and then to
    a single "Unable to parse" entry if the model never produces valid JSON,
    exactly as specified (this failure path is what structured_output_rate is
    meant to surface, not something to paper over).
    """
    from loop1.llm import call_llm_with_usage

    messages = [{"role": "user", "content": prompt}]
    total_prompt_tokens = 0
    total_latency_ms = 0.0
    raw_last = ""

    for attempt in range(_MAX_ATTEMPTS):
        t0 = time.monotonic()
        raw, usage = call_llm_with_usage(model=model, messages=messages)
        total_latency_ms += (time.monotonic() - t0) * 1000
        total_prompt_tokens += usage.prompt_tokens
        raw_last = raw

        parsed = _extract_and_validate(raw)
        if parsed is not None:
            return parsed, total_prompt_tokens, total_latency_ms, True

        if attempt < _MAX_ATTEMPTS - 1:
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": _JSON_RETRY_NUDGE},
            ]

    fallback_diff = _regex_fallback_differential(raw_last)
    if fallback_diff:
        parsed = {"differential": fallback_diff, "confidence": None, "reasoning": None}
    else:
        parsed = {
            "differential": [{"dx": "Unable to parse baseline output", "prob": 1.0}],
            "confidence": None,
            "reasoning": None,
        }
    return parsed, total_prompt_tokens, total_latency_ms, False


def _doctor_output_from_parsed(parsed: dict) -> dict:
    differential = [
        {"dx": str(d["dx"]), "prob": float(d["prob"])} for d in parsed["differential"]
    ]
    confidence = parsed.get("confidence")
    confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
    return {
        "turn_index": 0,
        "current_differential": differential,
        "biggest_uncertainty": None,
        "candidate_questions": [],
        "chosen_question": None,
        "rationale": parsed.get("reasoning") or "",
        "confidence_to_stop": confidence,
        "should_stop": True,
        "safety_flags": [],  # structurally true — the single-shot baseline has no active safety screening
    }


# ---------------------------------------------------------------------------
# Output paths + writers
# ---------------------------------------------------------------------------

def _session_jsonl_path(mode: str, patient_id: str) -> Path:
    return _DATA_DIR / "baseline_sessions" / f"mode_{mode}" / f"session_{patient_id}.jsonl"


def _eval_json_path(mode: str, patient_id: str) -> Path:
    return _DATA_DIR / "baseline_evals" / f"mode_{mode}" / f"eval_{patient_id}.json"


def _usage_json_path(mode: str, patient_id: str) -> Path:
    return _DATA_DIR / "baseline_usage" / f"mode_{mode}" / f"usage_{patient_id}.json"


def _write_session_jsonl(path: Path, session_id: str, doctor_output: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    turn_event = {
        "schema_version": _SESSION_SCHEMA_VERSION,
        "event_type": "turn_complete",
        "session_id": session_id,
        "turn_index": 0,
        "doctor_output": doctor_output,
        "patient_answer": "",
    }
    end_event = {
        "schema_version": _SESSION_SCHEMA_VERSION,
        "event_type": "session_end",
        "session_id": session_id,
        # Not "confidence_threshold" — the single-shot baseline always stops
        # after one turn regardless of confidence, so terminated_on_confidence
        # correctly evaluates to False for every baseline session.
        "termination_reason": "single_shot",
        "doctor_output": doctor_output,
    }
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(turn_event) + "\n")
        f.write(json.dumps(end_event) + "\n")


def _write_eval_json(path: Path, eval_result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(eval_result, f, indent=2)


def _write_usage_json(
    path: Path,
    entry: dict,
    session_id: str,
    mode: str,
    model: str,
    prompt_tokens: int,
    completion_tokens_estimated: int,
    latency_ms: float,
    parsed_ok: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "patient_id": entry["patient_id"],
        "session_id": session_id,
        "mode": mode,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens_estimated": completion_tokens_estimated,
        "completion_tokens_is_estimate": True,
        "latency_ms": round(latency_ms, 1),
        "parsed_ok": parsed_ok,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)


def _run_eval(entry: dict, session_path: Path, session_id: str) -> dict:
    from loop2.ddxplus.evaluator import evaluate_session

    patient = _build_patient(entry)
    result = evaluate_session(session_path, patient, session_id=session_id)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Per patient/mode driver
# ---------------------------------------------------------------------------

def _already_complete(mode: str, patient_id: str) -> bool:
    return (
        _session_jsonl_path(mode, patient_id).exists()
        and _eval_json_path(mode, patient_id).exists()
        and _usage_json_path(mode, patient_id).exists()
    )


def _process_patient_mode(entry: dict, mode: str, model: str, dry_run: bool) -> None:
    patient_id = entry["patient_id"]

    if _already_complete(mode, patient_id):
        _log.info("  [mode %s] %s already complete — skipping", mode, patient_id)
        return

    session_id = str(uuid.uuid4())
    session_path = _session_jsonl_path(mode, patient_id)
    eval_path = _eval_json_path(mode, patient_id)
    usage_path = _usage_json_path(mode, patient_id)

    if dry_run:
        doctor_output = {
            "turn_index": 0,
            "current_differential": [{"dx": "Dry Run Placeholder", "prob": 1.0}],
            "biggest_uncertainty": None,
            "candidate_questions": [],
            "chosen_question": None,
            "rationale": "dry run — no LLM call made",
            "confidence_to_stop": 0.5,
            "should_stop": True,
            "safety_flags": [],
        }
        _write_session_jsonl(session_path, session_id, doctor_output)
        _write_eval_json(eval_path, _run_eval(entry, session_path, session_id))
        _write_usage_json(
            usage_path, entry, session_id, mode, model,
            prompt_tokens=0, completion_tokens_estimated=0,
            latency_ms=0.0, parsed_ok=True,
        )
        _log.info("  [DRY RUN] [mode %s] wrote placeholder for %s", mode, patient_id)
        return

    if mode == "a":
        from loop2.ddxplus.patient_simulator import SimulatedPatient

        patient = _build_patient(entry)
        sim = SimulatedPatient(patient)
        # This call's own usage is NOT counted in mode a's token/cost/latency
        # figures — it belongs to the patient simulator, not the "traditional
        # LLM method" under test, and the actor-critic side pays the same
        # simulator cost without it being counted either. It's also uncounted
        # for free: SimulatedPatient uses call_llm() (no usage tracking).
        chief_complaint = sim.initial_complaint()
        prompt = _render_prompt_mode_a(entry, chief_complaint)
    else:
        prompt = _render_prompt_mode_b(entry)

    parsed, prompt_tokens, latency_ms, parsed_ok = _run_one_call(model, prompt)
    doctor_output = _doctor_output_from_parsed(parsed)
    completion_tokens_estimated = len(json.dumps(doctor_output)) // 4

    _write_session_jsonl(session_path, session_id, doctor_output)
    eval_result = _run_eval(entry, session_path, session_id)
    _write_eval_json(eval_path, eval_result)
    _write_usage_json(
        usage_path, entry, session_id, mode, model,
        prompt_tokens=prompt_tokens,
        completion_tokens_estimated=completion_tokens_estimated,
        latency_ms=latency_ms,
        parsed_ok=parsed_ok,
    )
    _log.info(
        "  [mode %s] %s: top1=%s parsed_ok=%s prompt_tokens=%d latency_ms=%.0f",
        mode, patient_id, eval_result["leading_diagnosis_correct"],
        parsed_ok, prompt_tokens, latency_ms,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Single-shot baseline eval harness (admin portal, Phase 1)"
    )
    parser.add_argument("--mode", choices=["a", "b", "both"], default="both")
    parser.add_argument("--max-patients", type=int, default=None)
    parser.add_argument("--model", default=None, help="Overrides config['models']['doctor']")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip real LLM calls; write structural placeholders instead",
    )
    args = parser.parse_args()

    from loop1.config import config

    model = args.model or config["models"]["doctor"]
    eval_set = _load_eval_set()
    if args.max_patients:
        eval_set = eval_set[: args.max_patients]

    modes = ["a", "b"] if args.mode == "both" else [args.mode]

    _log.info(
        "Running baseline eval: modes=%s model=%s patients=%d dry_run=%s",
        modes, model, len(eval_set), args.dry_run,
    )

    for mode in modes:
        _log.info("=== Mode %s ===", mode)
        for i, entry in enumerate(eval_set):
            _log.info(
                "[%d/%d] %s (%s)", i + 1, len(eval_set),
                entry["patient_id"], entry.get("disease", "?"),
            )
            try:
                _process_patient_mode(entry, mode, model, dry_run=args.dry_run)
            except Exception:
                _log.error(
                    "  FAILED for %s mode %s", entry["patient_id"], mode, exc_info=True
                )
                continue

    _log.info("Baseline eval complete.")


if __name__ == "__main__":
    main()
