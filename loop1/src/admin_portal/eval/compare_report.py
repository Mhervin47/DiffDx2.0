"""
Comparison report generator — admin portal, Phase 1.

Reads actor-critic eval results (from loop2.runners.phase7_eval), the
single-shot baseline results (from admin_portal/eval/baseline_eval.py), and
optional critic-critique / red-flag-label data, and computes every metric
defined in the build brief's Section 4. Writes the working copy to
loop1/data/admin_portal/comparison_report.json — publish_report.py then
copies it into loop1/web/admin_portal/data/, which is the only location the
router (and a real deployment) actually reads from.

Usage:
    python src/admin_portal/eval/compare_report.py
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# src/admin_portal/eval/compare_report.py -> eval -> admin_portal -> src -> loop1
# 4 parent hops — same depth as baseline_eval.py, verified for this file's
# actual location, not assumed by analogy.
_LOOP1_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_LOOP1_ROOT / "src"))

_EVAL_SET_PATH = _LOOP1_ROOT / "data" / "ddxplus_eval_set.json"
_PHASE7_SESSION_DIR = _LOOP1_ROOT / "data" / "phase7_sessions"
_PHASE7_CRITIQUE_DIR = _LOOP1_ROOT / "data" / "phase7_critiques"
_ADMIN_DATA_DIR = _LOOP1_ROOT / "data" / "admin_portal"
_RED_FLAG_LABELS_PATH = _ADMIN_DATA_DIR / "red_flag_labels.json"
_PRICING_PATH = Path(__file__).resolve().parent / "pricing.json"
_OUTPUT_PATH = _ADMIN_DATA_DIR / "comparison_report.json"

_BASELINE_MODES = ("a", "b")

# The five mandatory notes (build brief Section 5.3) — present verbatim (or
# near it) in every report, regardless of whether the numbers look good.
_NOTE_COST_UNDERCOUNT = (
    "mean_cost_per_session_usd_estimated for actor_critic is doctor-call cost only, sourced from "
    "this offline eval harness's own session logs (data/phase7_sessions/) — it excludes the "
    "critic, compressor, and profile_updater calls. This is a real undercount, not a deliberate "
    "scope choice: both the doctor and the critic run on every turn of a real deployed patient "
    "session today (see web/api_session.py's _fire_critic), and live per-session cost covering "
    "doctor, compressor, critic, and closing-turn calls together is tracked separately in the "
    "admin portal's Cost & Usage panel (sourced from the llm_usage_events table) — this offline "
    "benchmark figure just hasn't been extended to pull from that same source yet. True "
    "per-session cost for actor_critic is higher than the number shown here."
)
_NOTE_COMPLETION_ESTIMATED = (
    "All completion-token figures in this report are estimates "
    "(len(json.dumps(doctor_output)) // 4), not measured — loop1.llm never returns a "
    "completion-token count for any model call in this codebase, only prompt_tokens. Do not treat "
    "these as billed-token-accurate."
)
_NOTE_MODEL_PIPELINE_ASYMMETRY = (
    "The doctor model is held constant between actor_critic and both baseline modes "
    "(config['models']['doctor'], overridable via baseline_eval.py --model), so this is an "
    "architecture comparison, not a model comparison. The full per-turn model PIPELINE is NOT held "
    "constant, though: profile_updater and compressor models run on every turn of a real "
    "actor_critic session, and the critic model runs on every turn of a real deployed session too "
    "(see the cost note above) — none of them run in the baseline at all."
)


def _note_n_directional(n: int) -> str:
    return (
        f"This eval set has {n} patients. Treat any significance result here as directional, not "
        "confirmatory — a larger eval set would be needed for a strong statistical claim. This "
        "caveat applies regardless of whether the p-value looks significant or not."
    )


def _note_latency(mean_latency: float | None, detail: str | None) -> str:
    """Dynamic, not a fixed constant — Phase 1 always reported this as a
    hardcoded null (session.py had no timing instrumentation at all).
    Phase 2's optional Task 9 added it (loop1/src/loop1/session.py, tagged
    source='offline_cli' in llm_usage_events), so whether this is
    measurable now depends on whether that instrumentation exists AND
    phase7_eval.py has been run since. Both states are covered honestly —
    a stale 'still null' note would misreport a real measurement, and a
    silent switch to reporting a number would hide that it only covers the
    doctor model's own call, not profile_updater/compressor/critic."""
    if mean_latency is None:
        base = (
            "actor_critic's mean_latency_ms_per_session is null. Either loop1/src/loop1/session.py "
            "has not been instrumented (Phase 2's optional Task 9) or phase7_eval.py has not been "
            "run since it was — this report has no measured latency data to show either way. "
            "Baseline latency figures are real wall-clock measurements and are not directly "
            "comparable to a missing number."
        )
    else:
        base = (
            "actor_critic's mean_latency_ms_per_session IS measured here, via Phase 2's optional "
            "Task 9 instrumentation in loop1/src/loop1/session.py (source='offline_cli' in "
            "llm_usage_events) — but it covers only the doctor model's own call. The same "
            "profile_updater/compressor/critic gaps described in the cost note above apply to "
            "latency too: none of those calls are timed, so true per-session latency is higher "
            "than shown."
        )
    if detail:
        base += f" {detail}"
    return base


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load_eval_set() -> list[dict]:
    if not _EVAL_SET_PATH.exists():
        raise SystemExit(
            f"Eval set not found at {_EVAL_SET_PATH}.\n"
            "Run `python scripts/curate_eval_set.py` from inside loop1/ first to generate it."
        )
    with open(_EVAL_SET_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_actor_critic_evals() -> list[dict]:
    if not _PHASE7_SESSION_DIR.exists():
        raise SystemExit(
            f"No actor-critic eval results found at {_PHASE7_SESSION_DIR}.\n"
            "Run `python -m loop2.runners.phase7_eval` from inside loop1/ first."
        )
    evals = []
    for path in sorted(_PHASE7_SESSION_DIR.glob("eval_*.json")):
        with open(path, encoding="utf-8") as f:
            evals.append(json.load(f))
    if not evals:
        raise SystemExit(
            f"No eval_*.json files found under {_PHASE7_SESSION_DIR}.\n"
            "Run `python -m loop2.runners.phase7_eval` from inside loop1/ first."
        )
    return evals


def _actor_critic_token_estimate(patient_id: str) -> tuple[int | None, int | None]:
    """Sum real prompt_tokens and estimated completion tokens across a session's turn_complete events."""
    session_path = _PHASE7_SESSION_DIR / f"session_{patient_id}.jsonl"
    if not session_path.exists():
        return None, None
    prompt_sum = 0
    completion_sum = 0
    found_any = False
    with open(session_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("event_type") != "turn_complete":
                continue
            found_any = True
            prompt_sum += int(event.get("prompt_tokens", 0) or 0)
            completion_sum += len(json.dumps(event.get("doctor_output", {}))) // 4
    if not found_any:
        return None, None
    return prompt_sum, completion_sum


def _actor_critic_latency_stats(session_ids: list[str]) -> tuple[float | None, str | None]:
    """Mean per-session latency for actor_critic sessions, sourced from
    llm_usage_events (Postgres, via Phase 2's Decision Gate D1 Option B) —
    NOT from any file this offline tool otherwise reads. This is a
    deliberate, narrow live-DB dependency added only for this one field:
    everything else in this script stays pure-file-based. Falls back to
    (None, explanatory-detail) at every failure point rather than raising —
    this offline report must still generate with no Postgres reachable at
    all, exactly as it did before this field existed.

    Per-session latency is the SUM of that session's turn latencies (a
    session is many sequential turns; the accuracy-relevant, human-facing
    number is total time, not one turn), then MEAN across sessions —
    matching how mean_prompt_tokens_per_session already sums per session
    before averaging.
    """
    if not session_ids:
        return None, "No actor-critic sessions to look up."

    try:
        from sqlalchemy import select

        from diffdx.db.engine import get_sessionmaker
        from diffdx.db.models.usage import LlmUsageEvent
    except Exception as exc:
        return None, f"Latency lookup unavailable — could not import diffdx.db ({exc})."

    try:
        with get_sessionmaker()() as db:
            rows = db.execute(
                select(LlmUsageEvent.session_id, LlmUsageEvent.latency_ms).where(
                    LlmUsageEvent.source == "offline_cli",
                    LlmUsageEvent.session_id.in_(session_ids),
                )
            ).all()
    except Exception as exc:
        return None, f"Latency lookup failed — Postgres unreachable or not migrated ({exc})."

    if not rows:
        return None, (
            "No offline_cli usage records found for these sessions in llm_usage_events."
        )

    per_session: dict[str, float] = {}
    for sid, latency in rows:
        if latency is None:
            continue
        per_session[sid] = per_session.get(sid, 0.0) + latency

    if not per_session:
        return None, "Usage records found for these sessions, but none carried a measured latency value."

    covered = len(per_session)
    total = len(set(session_ids))
    mean_latency = sum(per_session.values()) / covered
    detail = None
    if covered < total:
        detail = (
            f"Latency measured for {covered}/{total} sessions only — the rest have no "
            "offline_cli usage records, likely run before the instrumentation existed."
        )
    return mean_latency, detail


def _load_baseline_evals(mode: str) -> list[dict]:
    eval_dir = _ADMIN_DATA_DIR / "baseline_evals" / f"mode_{mode}"
    if not eval_dir.exists():
        return []
    evals = []
    for path in sorted(eval_dir.glob("eval_*.json")):
        with open(path, encoding="utf-8") as f:
            evals.append(json.load(f))
    return evals


def _load_baseline_usage(mode: str) -> list[dict]:
    usage_dir = _ADMIN_DATA_DIR / "baseline_usage" / f"mode_{mode}"
    if not usage_dir.exists():
        return []
    records = []
    for path in sorted(usage_dir.glob("usage_*.json")):
        with open(path, encoding="utf-8") as f:
            records.append(json.load(f))
    return records


def _load_critiques() -> list | None:
    if not _PHASE7_CRITIQUE_DIR.exists():
        return None
    from loop2.critic.aggregator import load_critiques

    all_critiques = []
    paths = sorted(_PHASE7_CRITIQUE_DIR.glob("critique_*.jsonl"))
    if not paths:
        return None
    for path in paths:
        all_critiques.extend(load_critiques(path))
    return all_critiques


def _load_red_flag_labels() -> list[dict] | None:
    if not _RED_FLAG_LABELS_PATH.exists():
        return None
    with open(_RED_FLAG_LABELS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_pricing() -> dict:
    if not _PRICING_PATH.exists():
        raise SystemExit(f"pricing.json not found at {_PRICING_PATH} — this ships with the repo.")
    with open(_PRICING_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Shared metric helpers (Section 4.1)
# ---------------------------------------------------------------------------

def _fraction(correct: int, total: int) -> dict[str, Any]:
    return {"correct": correct, "total": total, "pct": (correct / total if total else None)}


def _mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def _accuracy_block(evals: list[dict]) -> dict[str, Any]:
    n = len(evals)
    top1 = sum(1 for e in evals if e.get("leading_diagnosis_correct"))
    top3 = sum(1 for e in evals if e.get("top3_contains_truth"))
    overlaps = [e.get("differential_overlap") for e in evals]
    return {
        "n": n,
        "top1_accuracy": _fraction(top1, n),
        "top3_accuracy": _fraction(top3, n),
        "mean_differential_overlap": _mean(overlaps),
    }


def _by_stratum(evals: list[dict], eval_set_by_pid: dict[str, dict]) -> dict[str, Any]:
    buckets: dict[str, list[dict]] = {"in_pool": [], "out_of_pool": [], "ambiguous": []}
    for e in evals:
        entry = eval_set_by_pid.get(e["patient_id"])
        stratum = entry.get("stratum") if entry else None
        if stratum in buckets:
            buckets[stratum].append(e)
    return {name: _accuracy_block(evs) for name, evs in buckets.items()}


def _lookup_pricing(pricing: dict, model: str) -> dict:
    return pricing.get(model, pricing["_default"])


def _mean_cost(mean_prompt: float | None, mean_completion: float | None, model: str, pricing: dict) -> float | None:
    if mean_prompt is None or mean_completion is None:
        return None
    rates = _lookup_pricing(pricing, model)
    return (mean_prompt / 1000 * rates["input_usd_per_1k"]) + (mean_completion / 1000 * rates["output_usd_per_1k"])


def _cost_per_correct(mean_cost: float | None, top1_pct: float | None) -> dict[str, Any]:
    if mean_cost is None:
        return {"value": None, "note": "no cost figure available for this system"}
    if not top1_pct:
        return {"value": None, "note": "undefined — zero correct diagnoses in this sample"}
    return {"value": mean_cost / top1_pct, "note": None}


def _build_actor_critic_block(evals: list[dict], eval_set_by_pid: dict, pricing: dict, doctor_model: str) -> dict:
    acc = _accuracy_block(evals)
    prompt_vals, completion_vals = [], []
    for e in evals:
        p, c = _actor_critic_token_estimate(e["patient_id"])
        prompt_vals.append(p)
        completion_vals.append(c)
    mean_prompt = _mean(prompt_vals)
    mean_completion = _mean(completion_vals)
    mean_cost = _mean_cost(mean_prompt, mean_completion, doctor_model, pricing)

    session_ids = [e["session_id"] for e in evals]
    mean_latency, latency_detail = _actor_critic_latency_stats(session_ids)

    return {
        "n": acc["n"],
        "top1_accuracy": acc["top1_accuracy"],
        "top3_accuracy": acc["top3_accuracy"],
        "mean_differential_overlap": acc["mean_differential_overlap"],
        "by_stratum": _by_stratum(evals, eval_set_by_pid),
        "structured_output_rate": {
            "value": 1.0,
            "note": (
                "schema-enforced by construction — the actor's doctor-turn output is a validated "
                "pydantic model, it cannot produce unstructured output."
            ),
        },
        "mean_prompt_tokens_per_session": mean_prompt,
        "mean_completion_tokens_estimated_per_session": mean_completion,
        "mean_cost_per_session_usd_estimated": mean_cost,
        "mean_latency_ms_per_session": {
            "value": round(mean_latency, 1) if mean_latency is not None else None,
            "note": _note_latency(mean_latency, latency_detail),
        },
        "cost_per_correct_diagnosis_usd": _cost_per_correct(mean_cost, acc["top1_accuracy"]["pct"]),
        "model": doctor_model,
    }


def _build_baseline_block(
    mode: str, evals: list[dict], usage: list[dict], eval_set_by_pid: dict, pricing: dict
) -> dict | None:
    if not evals or not usage:
        return None

    acc = _accuracy_block(evals)
    parsed_ok_rate = sum(1 for u in usage if u.get("parsed_ok")) / len(usage)
    mean_prompt = _mean([u.get("prompt_tokens") for u in usage])
    mean_completion = _mean([u.get("completion_tokens_estimated") for u in usage])
    mean_latency = _mean([u.get("latency_ms") for u in usage])
    model = usage[0].get("model", "unknown")
    mean_cost = _mean_cost(mean_prompt, mean_completion, model, pricing)

    return {
        "n": acc["n"],
        "top1_accuracy": acc["top1_accuracy"],
        "top3_accuracy": acc["top3_accuracy"],
        "mean_differential_overlap": acc["mean_differential_overlap"],
        "by_stratum": _by_stratum(evals, eval_set_by_pid),
        "structured_output_rate": {"value": parsed_ok_rate, "note": None},
        "mean_prompt_tokens_per_session": mean_prompt,
        "mean_completion_tokens_estimated_per_session": mean_completion,
        "mean_cost_per_session_usd_estimated": mean_cost,
        "mean_latency_ms_per_session": {"value": mean_latency, "note": None},
        "cost_per_correct_diagnosis_usd": _cost_per_correct(mean_cost, acc["top1_accuracy"]["pct"]),
        "model": model,
    }


# ---------------------------------------------------------------------------
# Statistical significance (Section 4.2) — exact McNemar, stdlib only
# ---------------------------------------------------------------------------

def exact_mcnemar_pvalue(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) * (0.5 ** n)
    return min(1.0, 2 * tail)


def _significance(actor_evals: list[dict], baseline_evals: list[dict] | None) -> dict | None:
    if not baseline_evals:
        return None
    actor_by_pid = {e["patient_id"]: bool(e.get("leading_diagnosis_correct")) for e in actor_evals}
    baseline_by_pid = {e["patient_id"]: bool(e.get("leading_diagnosis_correct")) for e in baseline_evals}
    common = sorted(set(actor_by_pid) & set(baseline_by_pid))
    if not common:
        return None

    b = sum(1 for pid in common if actor_by_pid[pid] and not baseline_by_pid[pid])
    c = sum(1 for pid in common if not actor_by_pid[pid] and baseline_by_pid[pid])
    n_common = len(common)
    actor_pct = sum(1 for pid in common if actor_by_pid[pid]) / n_common
    baseline_pct = sum(1 for pid in common if baseline_by_pid[pid]) / n_common

    return {
        "n": n_common,
        "b": b,
        "c": c,
        "n_discordant": b + c,
        "p_value": exact_mcnemar_pvalue(b, c),
        "top1_delta_pp": (actor_pct - baseline_pct) * 100,
        "note": _note_n_directional(n_common),
    }


# ---------------------------------------------------------------------------
# Safety recall (Section 4.3)
# ---------------------------------------------------------------------------

def _safety_block(
    actor_evals: list[dict],
    baseline_a_evals: list[dict],
    baseline_b_evals: list[dict],
    red_flag_labels: list[dict] | None,
    critiques: list | None,
) -> dict | None:
    if red_flag_labels is None:
        return None

    from admin_portal.eval.labeling.red_flag_labels import _AUTO_LABEL_SOURCE

    red_flag_ids = {r["patient_id"] for r in red_flag_labels if r.get("has_red_flag_feature")}

    def _recall(evals: list[dict]) -> dict:
        subset = [e for e in evals if e["patient_id"] in red_flag_ids]
        correct = sum(1 for e in subset if e.get("top3_contains_truth"))
        return _fraction(correct, len(subset))

    critic_missed_rate: dict | None = None
    if critiques is not None:
        sessions_with_missed = {
            c.session_id for c in critiques if c.weakness_category == "missed_red_flag"
        }
        actor_session_by_pid = {e["patient_id"]: e["session_id"] for e in actor_evals}
        red_flag_sessions = [
            actor_session_by_pid[pid] for pid in red_flag_ids if pid in actor_session_by_pid
        ]
        if red_flag_sessions:
            n_missed = sum(1 for sid in red_flag_sessions if sid in sessions_with_missed)
            critic_missed_rate = {
                "value": n_missed / len(red_flag_sessions),
                "total": len(red_flag_sessions),
                "available_for": ["actor_critic"],
            }

    n_auto = sum(1 for r in red_flag_labels if r.get("labeled_by") == _AUTO_LABEL_SOURCE)
    n_override = len(red_flag_labels) - n_auto

    return {
        "red_flag_patient_ids": sorted(red_flag_ids),
        "red_flag_top3_recall": {
            "actor_critic": _recall(actor_evals),
            "baseline_mode_a": _recall(baseline_a_evals) if baseline_a_evals else None,
            "baseline_mode_b": _recall(baseline_b_evals) if baseline_b_evals else None,
        },
        "critic_missed_red_flag_rate": critic_missed_rate,
        "label_provenance": {
            "automatic": n_auto,
            "override": n_override,
            "automatic_source": _AUTO_LABEL_SOURCE,
        },
    }


# ---------------------------------------------------------------------------
# Reasoning quality / cross-reference (optional)
# ---------------------------------------------------------------------------

def _reasoning_quality_and_cross_ref(
    critiques: list | None, actor_evals: list[dict]
) -> tuple[dict | None, list, str | None]:
    if critiques is None:
        note = (
            "No critique data found under data/phase7_critiques/ — reasoning-quality section "
            "omitted. Run `python -m loop2.runners.phase7_eval` (without --skip-critique) to "
            "generate it."
        )
        return None, [], note

    from loop2.critic.aggregator import aggregate_critiques, cross_reference_failures

    agg = aggregate_critiques(critiques)
    cross_ref = cross_reference_failures(critiques, actor_evals)
    return agg, cross_ref, None


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_report() -> dict:
    from loop1.config import config

    doctor_model = config["models"]["doctor"]
    pricing = _load_pricing()

    eval_set = _load_eval_set()
    eval_set_by_pid = {e["patient_id"]: e for e in eval_set}

    actor_evals = _load_actor_critic_evals()

    baseline_evals = {m: _load_baseline_evals(m) for m in _BASELINE_MODES}
    baseline_usage = {m: _load_baseline_usage(m) for m in _BASELINE_MODES}

    critiques = _load_critiques()
    red_flag_labels = _load_red_flag_labels()

    actor_critic_block = _build_actor_critic_block(actor_evals, eval_set_by_pid, pricing, doctor_model)
    systems: dict[str, Any] = {"actor_critic": actor_critic_block}
    notes: list[str] = [
        _NOTE_COST_UNDERCOUNT,
        # Reuses the exact note already computed for the per-field value
        # above — one source of truth, not a second hardcoded copy that
        # could drift from whether latency actually ended up measured.
        actor_critic_block["mean_latency_ms_per_session"]["note"],
        _note_n_directional(len(eval_set)),
        _NOTE_COMPLETION_ESTIMATED,
        _NOTE_MODEL_PIPELINE_ASYMMETRY,
    ]

    significance: dict[str, Any] = {}
    for mode in _BASELINE_MODES:
        key = f"baseline_mode_{mode}"
        block = _build_baseline_block(mode, baseline_evals[mode], baseline_usage[mode], eval_set_by_pid, pricing)
        systems[key] = block
        if block is None:
            notes.append(
                f"{key}: no baseline eval data found under data/admin_portal/baseline_evals/mode_{mode}/ "
                f"— run `python src/admin_portal/eval/baseline_eval.py --mode {mode}` first. This system's "
                "metrics and significance test are omitted, not fabricated as zero/null-looking-like-a-tie."
            )
            significance[f"actor_critic_vs_{key}"] = None
        else:
            significance[f"actor_critic_vs_{key}"] = _significance(actor_evals, baseline_evals[mode])

    safety = _safety_block(actor_evals, baseline_evals["a"], baseline_evals["b"], red_flag_labels, critiques)
    if safety is None:
        notes.append(
            "safety: red_flag_labels.json not found — run "
            "`python src/admin_portal/eval/labeling/red_flag_labels.py` to generate it (no clinical "
            "judgment required for the default pass). Safety-recall section omitted, not fabricated."
        )

    reasoning_quality, cross_reference, reasoning_note = _reasoning_quality_and_cross_ref(critiques, actor_evals)
    if reasoning_note:
        notes.append(reasoning_note)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "eval_set_size": len(eval_set),
        "systems": systems,
        "significance": significance,
        "safety": safety,
        "reasoning_quality": reasoning_quality,
        "cross_reference": cross_reference,
        "notes": notes,
    }


def _print_summary(report: dict) -> None:
    print(f"\nComparison report generated: {report['generated_at']}")
    print(f"Eval set size: {report['eval_set_size']}")
    for name, block in report["systems"].items():
        if block is None:
            print(f"  {name}: NO DATA")
            continue
        top1 = block["top1_accuracy"]
        print(
            f"  {name}: top1={top1['correct']}/{top1['total']} "
            f"({top1['pct']:.0%})" if top1["pct"] is not None else f"  {name}: top1=n/a"
        )
    for key, sig in report["significance"].items():
        if sig is None:
            continue
        print(f"  {key}: p={sig['p_value']:.4f} delta={sig['top1_delta_pp']:+.1f}pp (n={sig['n']}, directional)")
    if report["safety"] is None:
        print("  safety: not computed (no red_flag_labels.json)")
    if report["reasoning_quality"] is None:
        print("  reasoning_quality: not computed (no critique data)")
    print(f"\nNotes ({len(report['notes'])}):")
    for n in report["notes"]:
        print(f"  - {n[:100]}{'...' if len(n) > 100 else ''}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    report = build_report()
    _ADMIN_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    _log.info("Report written to %s", _OUTPUT_PATH)
    _print_summary(report)


if __name__ == "__main__":
    main()
