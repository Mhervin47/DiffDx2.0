"""
Red-flag safety labeling — admin portal, Phase 1.

Produces has_red_flag_feature: bool per eval-set patient, without requiring
whoever runs this to personally exercise clinical judgment.

Step 0 (per the build brief, Section 5.4): before hand-building a disease
list, check whether the raw DDXPlus dataset ships a condition-level
severity/acuity rating. It does — loop1/data/DDXPlus_Raw/release_conditions.json
has a "severity" field per condition (1 = most acute, 5 = least acute, per the
DDXPlus dataset's own scale). That is a real, dataset-provided label, and per
the brief it is used here INSTEAD OF a hand-picked _EMERGENT_DISEASES list.

This directly resolves the completeness gap the brief flagged in its earlier
pre-build review (Guillain-Barre syndrome and Acute pulmonary edema being
absent from any hand-picked list): both appear in release_conditions.json
with real severity values (2 and 1 respectively) and are picked up
automatically here, with no manual curation needed.

The severity CUTOFF chosen to mean "red flag" (--severity-max, default 2) is
still an editorial judgment call, exactly like the brief warned the hand-list
approach would be — it is a CLI flag specifically so that call is visible and
adjustable, not buried. Severity <=1 alone leaves only ~2 red-flag patients in
a 20-patient eval set, too few for any stratified recall number to mean much;
severity <=2 keeps the "cannot-miss" tier (ACS spectrum, PE, GBS, pneumothorax)
while staying dataset-driven rather than hand-picked.

Usage:
    python src/admin_portal/eval/labeling/red_flag_labels.py [--severity-max N]
    python src/admin_portal/eval/labeling/red_flag_labels.py --list
    python src/admin_portal/eval/labeling/red_flag_labels.py --interactive
    python src/admin_portal/eval/labeling/red_flag_labels.py \
        --set test_012345 true --labeled-by "Dr. Jane Doe" --note "confirmed on chart review"
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# src/admin_portal/eval/labeling/red_flag_labels.py
#   -> labeling -> eval -> admin_portal -> src -> loop1   (5 parent hops)
# One level deeper than baseline_eval.py (which needs 4) — this is exactly
# the off-by-one the build brief warns is easy to get wrong after a folder
# move; verified by counting directories in the actual final path, not
# copy-pasted from baseline_eval.py's 4-hop snippet.
_LOOP1_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent

_EVAL_SET_PATH = _LOOP1_ROOT / "data" / "ddxplus_eval_set.json"
_CONDITIONS_PATH = _LOOP1_ROOT / "data" / "DDXPlus_Raw" / "release_conditions.json"
_LABELS_PATH = _LOOP1_ROOT / "data" / "admin_portal" / "red_flag_labels.json"

_DEFAULT_SEVERITY_MAX = 2
_AUTO_LABEL_SOURCE = "ddxplus_severity_v1"
# compare_report.py's label_provenance must treat this exact string as the
# "automatic" bucket (the build brief's own example used "disease_list_v1",
# written for the hand-picked-list branch this script didn't need to take —
# keep this constant name in sync between the two files).


# ---------------------------------------------------------------------------
# Data loading
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


def _load_severity_by_name() -> dict[str, tuple[str, int | None]]:
    """Case-insensitive disease name -> (canonical_name, severity|None)."""
    if not _CONDITIONS_PATH.exists():
        raise SystemExit(
            f"DDXPlus condition metadata not found at {_CONDITIONS_PATH}.\n"
            "This file ships with the DDXPlus_Raw dataset (release_conditions.json) "
            "— confirm the dataset was extracted correctly under loop1/data/DDXPlus_Raw/."
        )
    with open(_CONDITIONS_PATH, encoding="utf-8") as f:
        conditions: dict[str, Any] = json.load(f)
    return {
        name.strip().lower(): (name, c.get("severity"))
        for name, c in conditions.items()
    }


def _load_existing_labels() -> dict[str, dict]:
    if not _LABELS_PATH.exists():
        return {}
    with open(_LABELS_PATH, encoding="utf-8") as f:
        records: list[dict] = json.load(f)
    return {r["patient_id"]: r for r in records}


def _save_labels(labels: dict[str, dict]) -> None:
    _LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ordered = [labels[pid] for pid in labels]
    with open(_LABELS_PATH, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=2)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _classify(
    pathology: str, severity_by_name: dict[str, tuple[str, int | None]], severity_max: int
) -> tuple[bool, int | None]:
    """Return (has_red_flag_feature, severity_value_or_None_if_unmatched)."""
    match = severity_by_name.get(pathology.strip().lower())
    if match is None:
        _log.warning(
            "No DDXPlus severity entry found for pathology %r — defaulting to "
            "has_red_flag_feature=False. Check release_conditions.json for a "
            "name mismatch if this is unexpected.",
            pathology,
        )
        return False, None
    _, severity = match
    if severity is None:
        return False, None
    return severity <= severity_max, severity


def _auto_record(entry: dict, severity_by_name: dict, severity_max: int) -> dict:
    pathology = entry["ground_truth_pathology"]
    has_flag, severity = _classify(pathology, severity_by_name, severity_max)
    return {
        "patient_id": entry["patient_id"],
        "disease": pathology,
        "stratum": entry.get("stratum", "unknown"),
        "has_red_flag_feature": has_flag,
        "labeled_by": _AUTO_LABEL_SOURCE,
        "notes": f"DDXPlus severity={severity} (<= {severity_max} => red flag); dataset-provided, not hand-picked.",
        "labeled_at": datetime.now(timezone.utc).isoformat(),
        "severity": severity,
        "severity_source": "release_conditions.json",
    }


def run_auto_pass(severity_max: int) -> dict[str, dict]:
    eval_set = _load_eval_set()
    severity_by_name = _load_severity_by_name()
    existing = _load_existing_labels()

    labels: dict[str, dict] = {}
    for entry in eval_set:
        pid = entry["patient_id"]
        prior = existing.get(pid)
        # A real human override (labeled_by != the automatic source) is never
        # silently clobbered by re-running the automatic pass.
        if prior is not None and prior.get("labeled_by") != _AUTO_LABEL_SOURCE:
            labels[pid] = prior
            continue
        record = _auto_record(entry, severity_by_name, severity_max)
        labels[pid] = record
        _save_labels(labels)  # incremental save, matches this repo's resumability convention
        _log.info(
            "  %-12s %-45s severity=%s -> red_flag=%s",
            entry.get("stratum", "?"), entry["ground_truth_pathology"],
            record["severity"], record["has_red_flag_feature"],
        )

    _save_labels(labels)
    return labels


# ---------------------------------------------------------------------------
# Interactive review
# ---------------------------------------------------------------------------

def run_interactive_pass(severity_max: int) -> dict[str, dict]:
    from rich.console import Console
    from rich.prompt import Confirm, Prompt

    console = Console()
    labels = run_auto_pass(severity_max)

    console.print(
        "\n[bold]Interactive red-flag review[/bold] — auto-classification shown per "
        "patient; default is to keep it. Answering 'yes' lets you override.\n"
    )
    for pid, record in labels.items():
        console.print(
            f"[cyan]{pid}[/cyan]  {record['disease']}  "
            f"(severity={record['severity']}, stratum={record['stratum']})  "
            f"-> auto has_red_flag_feature=[bold]{record['has_red_flag_feature']}[/bold]"
        )
        if record["labeled_by"] != _AUTO_LABEL_SOURCE:
            console.print(f"  [dim]already has a real override from '{record['labeled_by']}' — skipping[/dim]")
            continue
        if Confirm.ask("  Override this label?", default=False):
            new_val = Confirm.ask("  Set has_red_flag_feature to True?", default=record["has_red_flag_feature"])
            labeled_by = Prompt.ask("  Your name / identifier", default="clinician_review")
            note = Prompt.ask("  Note (optional)", default="")
            labels[pid] = {
                **record,
                "has_red_flag_feature": new_val,
                "labeled_by": labeled_by,
                "notes": note or None,
                "labeled_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_labels(labels)

    return labels


# ---------------------------------------------------------------------------
# Direct override
# ---------------------------------------------------------------------------

def apply_direct_override(
    patient_id: str, value: bool, labeled_by: str, note: str | None, severity_max: int
) -> None:
    labels = run_auto_pass(severity_max) if not _LABELS_PATH.exists() else _load_existing_labels()
    if patient_id not in labels:
        # Not in the current label set yet — run the auto pass to establish
        # disease/stratum context for this patient before overriding it.
        labels = run_auto_pass(severity_max)
    if patient_id not in labels:
        raise SystemExit(f"Patient {patient_id!r} not found in the eval set — check the ID.")

    record = labels[patient_id]
    labels[patient_id] = {
        **record,
        "has_red_flag_feature": value,
        "labeled_by": labeled_by,
        "notes": note,
        "labeled_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_labels(labels)
    _log.info("Override applied: %s -> has_red_flag_feature=%s (labeled_by=%s)", patient_id, value, labeled_by)


# ---------------------------------------------------------------------------
# --list
# ---------------------------------------------------------------------------

def print_summary() -> None:
    labels = _load_existing_labels()
    if not labels:
        print(f"No labels found at {_LABELS_PATH}. Run this script without flags to generate them.")
        return

    by_stratum: dict[str, list[dict]] = {}
    for record in labels.values():
        by_stratum.setdefault(record.get("stratum", "unknown"), []).append(record)

    print(f"Labels file: {_LABELS_PATH}")
    print(f"Total patients labeled: {len(labels)}\n")

    for stratum, records in sorted(by_stratum.items()):
        n_flag = sum(1 for r in records if r["has_red_flag_feature"])
        print(f"  {stratum:12s} n={len(records):2d}  red_flag=True: {n_flag}")

    n_auto = sum(1 for r in labels.values() if r["labeled_by"] == _AUTO_LABEL_SOURCE)
    n_override = len(labels) - n_auto
    print(f"\nLabel provenance: {n_auto} automatic ({_AUTO_LABEL_SOURCE}), {n_override} real override(s)")
    if n_override:
        for r in labels.values():
            if r["labeled_by"] != _AUTO_LABEL_SOURCE:
                print(f"    override: {r['patient_id']} by {r['labeled_by']!r} -> {r['has_red_flag_feature']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Red-flag safety labeling (admin portal, Phase 1)")
    parser.add_argument(
        "--severity-max", type=int, default=_DEFAULT_SEVERITY_MAX,
        help=(
            "DDXPlus severity <= this value counts as a red flag "
            "(1=most acute, 5=least acute). Default 2."
        ),
    )
    parser.add_argument("--interactive", action="store_true", help="Review/override the auto pass one patient at a time")
    parser.add_argument("--list", action="store_true", help="Print label counts per stratum and provenance")
    parser.add_argument("--set", nargs=2, metavar=("PATIENT_ID", "true|false"), help="Direct override for one patient")
    parser.add_argument("--labeled-by", default=None, help="Required with --set: who is making this override")
    parser.add_argument("--note", default=None, help="Optional note, used with --set")
    args = parser.parse_args()

    if args.list:
        print_summary()
        return

    if args.set:
        patient_id, raw_value = args.set
        if raw_value.lower() not in ("true", "false"):
            raise SystemExit("--set value must be 'true' or 'false'")
        if not args.labeled_by:
            raise SystemExit("--set requires --labeled-by so label provenance can be tracked")
        apply_direct_override(
            patient_id, raw_value.lower() == "true", args.labeled_by, args.note, args.severity_max
        )
        return

    if args.interactive:
        run_interactive_pass(args.severity_max)
        print_summary()
        return

    run_auto_pass(args.severity_max)
    print_summary()


if __name__ == "__main__":
    main()
