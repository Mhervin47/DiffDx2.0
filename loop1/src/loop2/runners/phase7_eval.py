"""
Phase 7 evaluation runner.

Runs all 20 DDXPlus patients end-to-end:
  1. Load patient from eval set
  2. Run a Loop 1 session driven by SimulatedPatient
  3. Save session JSONL
  4. Run ground-truth evaluator → DDXPlusEvalResult
  5. Run critic on each turn → TurnCritique
  6. Save critique JSONL
After all 20:
  7. Run aggregator on critique JSONL
  8. Generate phase7_findings.md

Usage:
    python -m loop2.runners.phase7_eval [--dry-run] [--max-patients N]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_EVAL_SET_PATH = _REPO_ROOT / "data" / "ddxplus_eval_set.json"
_SESSION_DIR = _REPO_ROOT / "data" / "phase7_sessions"
_CRITIQUE_DIR = _REPO_ROOT / "data" / "phase7_critiques"
_FINDINGS_PATH = _REPO_ROOT / "phase7_findings.md"


def _load_eval_set() -> list[dict]:
    with open(_EVAL_SET_PATH, encoding="utf-8") as f:
        return json.load(f)


def _session_jsonl_path(patient_id: str) -> Path:
    return _SESSION_DIR / f"session_{patient_id}.jsonl"


def _critique_jsonl_path(patient_id: str) -> Path:
    return _CRITIQUE_DIR / f"critique_{patient_id}.jsonl"


def _eval_json_path(patient_id: str) -> Path:
    return _SESSION_DIR / f"eval_{patient_id}.json"


def _already_complete(patient_id: str) -> bool:
    """Return True if this patient already has a complete session + eval + critique."""
    return (
        _session_jsonl_path(patient_id).exists()
        and _eval_json_path(patient_id).exists()
        and _critique_jsonl_path(patient_id).exists()
    )


def _build_patient_from_entry(entry: dict):
    """Reconstruct a DDXPlusPatient from an eval set JSON entry."""
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
            tuple(pair) for pair in entry["ground_truth_differential"]  # type: ignore[misc]
        ],
    )


def _run_patient_session(entry: dict, session_id: str, dry_run: bool = False) -> Path:
    """
    Run a full Loop 1 session driven by SimulatedPatient.
    Returns the path to the saved session JSONL.
    """
    if dry_run:
        # Checked BEFORE constructing SimulatedPatient/PatientProfile below —
        # neither is needed for the placeholder JSONL, and constructing the
        # profile requires sim.initial_complaint(), a real LLM call. Moved
        # here so --dry-run makes zero LLM calls, as its own docstring and
        # --help text promise (previously this branch ran after that call).
        _log.info("[DRY RUN] Would run session for %s", entry["patient_id"])
        # Write a minimal placeholder JSONL
        session_path = _session_jsonl_path(entry["patient_id"])
        session_path.parent.mkdir(parents=True, exist_ok=True)
        with open(session_path, "w") as f:
            f.write(json.dumps({
                "schema_version": "0.5.0",
                "event_type": "session_end",
                "session_id": session_id,
                "termination_reason": "max_turns",
                "doctor_output": {
                    "turn_index": 0,
                    "current_differential": [{"dx": "Unknown", "prob": 0.5}],
                    "biggest_uncertainty": "dry run",
                    "candidate_questions": ["placeholder"],
                    "chosen_question": "placeholder",
                    "rationale": "dry run",
                    "confidence_to_stop": 0.3,
                    "should_stop": False,
                    "safety_flags": [],
                },
            }) + "\n")
        return session_path

    from loop1.schemas import PatientProfile, Demographics
    from loop1.session import Session
    from loop2.ddxplus.patient_simulator import SimulatedPatient

    ddx_patient = _build_patient_from_entry(entry)
    sim = SimulatedPatient(ddx_patient)

    # Build a minimal PatientProfile for Loop 1 using the DDXPlus data
    profile = PatientProfile(
        session_id=session_id,
        demographics=Demographics(age=ddx_patient.age, sex=ddx_patient.sex),
        chief_complaint=sim.initial_complaint(),
    )

    patient_path = _session_jsonl_path(entry["patient_id"])

    # Skip session if already saved (resumability)
    if patient_path.exists():
        _log.info("Session already exists for %s — skipping session run.", entry["patient_id"])
        return patient_path

    # Override the logging path to save to phase7_sessions/ directory
    import loop1.config as lc
    original_session_dir = lc.config["logging"]["session_dir"]
    original_final_dir = lc.config["logging"]["final_dir"]
    lc.config["logging"]["session_dir"] = str(_SESSION_DIR)
    lc.config["logging"]["final_dir"] = str(_SESSION_DIR)
    try:
        session = Session(
            profile=profile,
            patient_source=sim,
            max_turns=12,
        )
        session.run()
    finally:
        lc.config["logging"]["session_dir"] = original_session_dir
        lc.config["logging"]["final_dir"] = original_final_dir

    # Session logger saves as session_{uuid}.jsonl; rename to patient-id-keyed path
    uuid_path = _SESSION_DIR / f"session_{session_id}.jsonl"
    if uuid_path.exists():
        uuid_path.rename(patient_path)
    return patient_path


def _read_session_events(session_path: Path) -> list[dict]:
    events = []
    with open(session_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _run_eval(entry: dict, session_path: Path, session_id: str) -> dict:
    from loop2.ddxplus.evaluator import evaluate_session

    ddx_patient = _build_patient_from_entry(entry)
    result = evaluate_session(session_path, ddx_patient, session_id=session_id)
    result_dict = result.model_dump()

    eval_path = _eval_json_path(entry["patient_id"])
    with open(eval_path, "w", encoding="utf-8") as f:
        json.dump(result_dict, f, indent=2)

    return result_dict


def _run_critique(entry: dict, session_path: Path, session_id: str) -> list[dict]:
    from loop2.critic.critic import critique_session

    events = _read_session_events(session_path)
    critiques = critique_session(events, session_id=session_id)

    critique_path = _critique_jsonl_path(entry["patient_id"])
    with open(critique_path, "w", encoding="utf-8") as f:
        for c in critiques:
            f.write(c.model_dump_json() + "\n")

    return [c.model_dump() for c in critiques]


def _load_all_critiques() -> list:
    from loop2.critic.aggregator import load_critiques

    all_critiques = []
    for path in sorted(_CRITIQUE_DIR.glob("critique_*.jsonl")):
        all_critiques.extend(load_critiques(path))
    return all_critiques


def _load_all_evals() -> list[dict]:
    evals = []
    for path in sorted(_SESSION_DIR.glob("eval_*.json")):
        with open(path, encoding="utf-8") as f:
            evals.append(json.load(f))
    return evals


def _generate_findings_report(
    eval_set: list[dict],
    all_evals: list[dict],
    agg_stats: dict,
    cross_ref: list[dict],
) -> str:
    lines: list[str] = [
        "# Phase 7 Findings Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "---",
        "",
        "## 1. Diagnostic Accuracy",
        "",
    ]

    # Per-stratum accuracy
    eval_by_patient = {e["patient_id"]: e for e in all_evals}
    strata = {"in_pool": [], "out_of_pool": [], "ambiguous": []}
    for entry in eval_set:
        pid = entry["patient_id"]
        ev = eval_by_patient.get(pid)
        if ev:
            strata[entry["stratum"]].append(ev)

    for stratum_name, evals in strata.items():
        if not evals:
            continue
        top1 = sum(1 for e in evals if e.get("leading_diagnosis_correct"))
        top3 = sum(1 for e in evals if e.get("top3_contains_truth"))
        n = len(evals)
        lines.append(f"### {stratum_name.replace('_', ' ').title()} (n={n})")
        lines.append(f"- Top-1 correct: {top1}/{n} ({top1/n:.0%})")
        lines.append(f"- Ground truth in top-3: {top3}/{n} ({top3/n:.0%})")
        lines.append("")

    # Per-disease breakdown
    lines += [
        "### Per-disease breakdown",
        "",
        "| Disease | Stratum | Top-1 Correct | In Top-3 | Turns | Confidence |",
        "|---------|---------|--------------|----------|-------|------------|",
    ]
    for entry in eval_set:
        pid = entry["patient_id"]
        ev = eval_by_patient.get(pid, {})
        top1 = "✓" if ev.get("leading_diagnosis_correct") else "✗"
        top3 = "✓" if ev.get("top3_contains_truth") else "✗"
        turns = ev.get("total_turns", "—")
        conf = f"{ev.get('final_confidence', 0):.2f}"
        disease = entry["disease"][:30]
        stratum = entry["stratum"]
        lines.append(f"| {disease} | {stratum} | {top1} | {top3} | {turns} | {conf} |")
    lines.append("")

    # 2. Question quality
    lines += [
        "---",
        "",
        "## 2. Question Quality (Critic)",
        "",
        f"- Total turns critiqued: {agg_stats.get('total_turns', 0)}",
        f"- Mean question quality: {agg_stats.get('mean_question_quality', 'N/A')}",
        f"- Median question quality: {agg_stats.get('median_question_quality', 'N/A')}",
        f"- Mean differential quality: {agg_stats.get('mean_differential_quality', 'N/A')}",
        f"- Mean reasoning quality: {agg_stats.get('mean_reasoning_quality', 'N/A')}",
        "",
        "### Confidence calibration distribution",
        "",
    ]
    for label, count in agg_stats.get("confidence_calibration_distribution", {}).items():
        lines.append(f"- {label}: {count}")
    lines.append("")

    lines += [
        "### Weakness category counts",
        "",
    ]
    for cat, count in sorted(
        agg_stats.get("weakness_category_counts", {}).items(),
        key=lambda x: -x[1],
    ):
        lines.append(f"- {cat}: {count}")
    lines.append("")

    # 3. Cross-reference
    lines += [
        "---",
        "",
        "## 3. Cross-Reference: Low Question Quality + Wrong Diagnosis",
        "",
        "Sessions where question quality was poor AND the diagnosis was wrong:",
        "",
        "| Patient | Mean Q Quality | Low-Quality Turns | Correct? | Weakness Categories |",
        "|---------|---------------|------------------|----------|---------------------|",
    ]
    for row in cross_ref:
        if row["leading_diagnosis_correct"] is False and (row["mean_question_quality"] or 1.0) < 0.6:
            pid = row["patient_id"][:20]
            mq = f"{row['mean_question_quality']:.2f}" if row["mean_question_quality"] else "—"
            lqt = str(row["low_quality_turns"])
            correct = "✗"
            cats = ", ".join(set(row["weakness_categories"]))
            lines.append(f"| {pid} | {mq} | {lqt} | {correct} | {cats} |")
    lines.append("")

    # 4. Failure modes
    lines += [
        "---",
        "",
        "## 4. Categorized Failure Modes",
        "",
    ]

    # Count failure mode categories from cross_ref
    wrong_no_exemplar = sum(
        1 for r in cross_ref
        if r["leading_diagnosis_correct"] is False and not r.get("in_exemplar_pool")
    )
    wrong_in_pool = sum(
        1 for r in cross_ref
        if r["leading_diagnosis_correct"] is False and r.get("in_exemplar_pool")
    )
    redundant = agg_stats.get("weakness_category_counts", {}).get("redundant_question", 0)
    missed_flag = agg_stats.get("weakness_category_counts", {}).get("missed_red_flag", 0)
    poor_diff = agg_stats.get("weakness_category_counts", {}).get("poor_differential", 0)

    lines += [
        f"- Wrong diagnosis, no matching exemplar: {wrong_no_exemplar}",
        f"- Wrong diagnosis, exemplar existed: {wrong_in_pool}",
        f"- Redundant questions (total turns): {redundant}",
        f"- Missed red flags (total turns): {missed_flag}",
        f"- Poor differential ordering (total turns): {poor_diff}",
        "",
        "---",
        "",
        "## 5. Top Weakness Texts",
        "",
    ]
    for item in agg_stats.get("top_weakness_texts", []):
        lines.append(f"- ({item['count']}×) {item['weakness']}")
    lines.append("")

    lines += [
        "---",
        "",
        "## 6. Phase 8 Recommendations",
        "",
    ]

    # Auto-generate recommendations based on data
    total = len(all_evals)
    top1_total = sum(1 for e in all_evals if e.get("leading_diagnosis_correct"))
    if total > 0:
        accuracy = top1_total / total
        if accuracy < 0.5:
            lines.append("- **Prompt iteration needed**: top-1 accuracy below 50% — doctor prompt is underperforming.")
        if wrong_no_exemplar > 3:
            lines.append(f"- **Expand exemplar pool**: {wrong_no_exemplar} wrong-diagnosis sessions had no matching exemplar.")
        if missed_flag > 5:
            lines.append(f"- **Targeted exemplars for red-flag screening**: {missed_flag} turns flagged for missed red flags.")
        if redundant > 5:
            lines.append(f"- **Deduplication in doctor prompt**: {redundant} turns flagged as redundant questions.")
    lines.append("")

    return "\n".join(lines)


def run_eval(
    dry_run: bool = False,
    max_patients: int | None = None,
    skip_critique: bool = False,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    _CRITIQUE_DIR.mkdir(parents=True, exist_ok=True)

    eval_set = _load_eval_set()
    if max_patients:
        eval_set = eval_set[:max_patients]

    total = len(eval_set)
    _log.info("Running Phase 7 eval on %d patients", total)

    all_eval_results: list[dict] = []
    all_critique_results: list[dict] = []

    for i, entry in enumerate(eval_set):
        patient_id = entry["patient_id"]
        _log.info("[%d/%d] Patient %s (%s — %s)", i + 1, total, patient_id, entry["stratum"], entry["disease"])

        if _already_complete(patient_id):
            _log.info("  Skipping (already complete)")
            # Load existing results
            with open(_eval_json_path(patient_id)) as f:
                all_eval_results.append(json.load(f))
            from loop2.critic.aggregator import load_critiques
            critiques = load_critiques(_critique_jsonl_path(patient_id))
            all_critique_results.extend([c.model_dump() for c in critiques])
            continue

        session_id = str(uuid.uuid4())

        try:
            # Step 1-2: Run session
            session_path = _run_patient_session(entry, session_id, dry_run=dry_run)
            _log.info("  Session saved: %s", session_path)

            # Step 3: Ground-truth eval
            eval_result = _run_eval(entry, session_path, session_id)
            all_eval_results.append(eval_result)
            _log.info(
                "  Eval: top1=%s top3=%s turns=%d",
                eval_result["leading_diagnosis_correct"],
                eval_result["top3_contains_truth"],
                eval_result["total_turns"],
            )

            # Step 4: Critique
            if not skip_critique:
                critiques = _run_critique(entry, session_path, session_id)
                all_critique_results.extend(critiques)
                _log.info("  Critiqued %d turns", len(critiques))

        except Exception as exc:
            _log.error("  FAILED for patient %s: %s", patient_id, exc, exc_info=True)
            continue

    # Step 5: Aggregate + report
    _log.info("Aggregating %d critique records across %d sessions", len(all_critique_results), total)
    from loop2.critic.aggregator import aggregate_critiques, cross_reference_failures
    from loop2.critic.critique_schema import TurnCritique

    all_critiques = [TurnCritique.model_validate(c) for c in all_critique_results]
    agg_stats = aggregate_critiques(all_critiques)
    cross_ref = cross_reference_failures(all_critiques, all_eval_results)

    report = _generate_findings_report(eval_set, all_eval_results, agg_stats, cross_ref)
    with open(_FINDINGS_PATH, "w", encoding="utf-8") as f:
        f.write(report)

    _log.info("Findings report written to %s", _FINDINGS_PATH)

    # Print summary
    total_done = len(all_eval_results)
    top1_correct = sum(1 for e in all_eval_results if e.get("leading_diagnosis_correct"))
    print(f"\nPhase 7 complete: {total_done}/{total} patients evaluated")
    print(f"Top-1 accuracy: {top1_correct}/{total_done} ({top1_correct/total_done:.0%})" if total_done else "")
    print(f"Mean question quality: {agg_stats.get('mean_question_quality', 'N/A')}")
    print(f"Findings report: {_FINDINGS_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 7 evaluation runner")
    parser.add_argument("--dry-run", action="store_true", help="Skip actual LLM calls")
    parser.add_argument("--max-patients", type=int, default=None)
    parser.add_argument("--skip-critique", action="store_true", help="Skip critic (eval only)")
    args = parser.parse_args()
    run_eval(dry_run=args.dry_run, max_patients=args.max_patients, skip_critique=args.skip_critique)


if __name__ == "__main__":
    main()
