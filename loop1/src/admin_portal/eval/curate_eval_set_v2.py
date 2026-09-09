"""
Curate a larger DDXPlus eval set — admin portal, offline tooling only.

Does NOT touch the existing, read-only loop1/scripts/curate_eval_set.py.
Writes to the exact same output path that script uses
(data/ddxplus_eval_set.json), because every downstream consumer
(phase7_eval.py, baseline_eval.py, compare_report.py, red_flag_labels.py)
already hardcodes that path — there's no reason to invent a second one, and
every consumer works generically off whatever's actually in the file
regardless of size or which script produced it.

Why a bigger set, and why this is purely an offline decision: this file
lives under loop1/data/, which the Dockerfile never copies into the
deployed image (see build brief Section 3.4/9.2) — the size of the eval set
has zero effect on what gets deployed. It only changes how much evidence
(and how much real LLM-call cost) a future real run of
loop2.runners.phase7_eval / baseline_eval.py produces.

Methodology, same as the original script:
  - in_pool: diseases that are BOTH a real DDXPlus pathology AND literally
    in loop2.ddxplus.loader.EXEMPLAR_POOL_DISEASES. Only 5 such diseases
    exist in this dataset at all (checked empirically against
    release_test_patients — the other 9 nominal exemplar diseases have no
    DDXPlus pathology): Pulmonary embolism, Pericarditis, SLE, Possible
    NSTEMI/STEMI, Anaphylaxis. "Unstable angina" is kept as a 6th, adjacent
    in_pool disease (same judgment call the original script already made —
    it does NOT satisfy is_in_exemplar_pool() itself, since it's not
    literally in the frozenset, but tests whether the stimulant_acs
    exemplar generalizes to a related ACS variant). To grow this stratum
    without reducing it to noise, each of these 6 diseases contributes 2
    patients instead of 1.
  - out_of_pool / ambiguous: same selection logic as the original
    (prefer_gt_top1 / prefer_tight), extended to roughly double the
    original disease lists, each still contributing 1 patient.

Run:
    python src/admin_portal/eval/curate_eval_set_v2.py

Output: loop1/data/ddxplus_eval_set.json (42 patients: 12 in_pool,
20 out_of_pool, 10 ambiguous), same schema as the original script's output.
"""
from __future__ import annotations

import ast
import json
import random
import sys
from pathlib import Path

# src/admin_portal/eval/curate_eval_set_v2.py -> eval -> admin_portal -> src -> loop1
_LOOP1_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_LOOP1_ROOT / "src"))

import pandas as pd

from loop2.ddxplus.loader import _load_evidences_ontology, _parse_patient_row, _DDXPLUS_DIR

SEED = 142  # distinct from the original script's SEED=42
random.seed(SEED)

# ---------------------------------------------------------------------------
# Selection targets — 2x the original disease counts, same stratification
# philosophy (in_pool prefers gt_top1; ambiguous prefers a tight top-2 gap)
# ---------------------------------------------------------------------------

# 2 patients drawn per disease here (see module docstring for why only 6
# diseases are available at all)
IN_POOL = [
    "Pulmonary embolism",
    "Pericarditis",
    "Possible NSTEMI / STEMI",
    "SLE",
    "Anaphylaxis",
    "Unstable angina",
]

# 1 patient drawn per disease — original 10 plus 10 more, spanning a range
# of severities (Ebola/Larygospasm at severity 1 down to Whooping cough at
# severity 4) and organ systems (airway, cardiac, neuromuscular, oncologic)
OUT_OF_POOL = [
    "Pneumonia",
    "Influenza",
    "Panic attack",
    "Atrial fibrillation",
    "Cluster headache",
    "Spontaneous pneumothorax",
    "Tuberculosis",
    "Bronchospasm / acute asthma exacerbation",
    "Guillain-Barré syndrome",
    "Acute pulmonary edema",
    "Myocarditis",
    "Boerhaave",
    "Epiglottitis",
    "Ebola",
    "Larygospasm",
    "Whooping cough",
    "Croup",
    "Bronchiolitis",
    "Myasthenia gravis",
    "Pancreatic neoplasm",
]

# 1 patient drawn per disease — original 5 plus 5 more, all selected for a
# tight top-2 differential gap
AMBIGUOUS = [
    "GERD",
    "Anemia",
    "Bronchitis",
    "PSVT",
    "Stable angina",
    "Acute COPD exacerbation / infection",
    "Chagas",
    "Scombroid food poisoning",
    "Acute rhinosinusitis",
    "Pulmonary neoplasm",
]

RATIONALES: dict[str, str] = {
    "Pulmonary embolism":
        "In-pool exemplar disease. Tests whether retrieval correctly surfaces PE exemplar "
        "and doctor applies DVT/travel/breathlessness reasoning pattern.",
    "Pericarditis":
        "In-pool exemplar disease. Pericarditis exemplar covers friction rub + positional pain; "
        "tests whether doctor screens for those features.",
    "Possible NSTEMI / STEMI":
        "In-pool exemplar disease (stimulant ACS exemplar). Tests ACS red-flag screening "
        "and appropriate confidence given high-stakes diagnosis.",
    "SLE":
        "In-pool exemplar disease. Multi-system presentation; tests whether doctor "
        "tracks malar rash + arthritis + renal features together.",
    "Anaphylaxis":
        "In-pool exemplar disease with its own dedicated Loop 1 exemplar. Omitted from the "
        "original 20-patient set despite being a genuine, real DDXPlus/exemplar overlap — "
        "most of the other 14 nominal exemplar diseases (CVST, Measles, Appendicitis, "
        "Cellulitis, Biliary colic, Cauda equina syndrome, DVT, Inflammatory back, "
        "Pregnancy) have no corresponding DDXPlus pathology at all. Tests airway/anaphylaxis "
        "red-flag screening directly against its own exemplar.",
    "Unstable angina":
        "Adjacent to in-pool ACS exemplar, not literally in EXEMPLAR_POOL_DISEASES itself. "
        "Tests whether the stimulant_acs exemplar retrieves and applies correctly to a "
        "related but distinct ACS variant.",
    "Pneumonia":
        "Out-of-pool. Common but absent from exemplar bank; tests generalisation "
        "to a frequent respiratory condition.",
    "Influenza":
        "Out-of-pool. High-volume upper-respiratory; tests whether doctor differentiates "
        "from URTI/bronchitis without exemplar guidance.",
    "Panic attack":
        "Out-of-pool. Psychiatric mimicker of cardiac disease; tests differential breadth "
        "when no exemplar covers this presentation.",
    "Atrial fibrillation":
        "Out-of-pool. Cardiac arrhythmia; tests palpitation + stroke-risk differential "
        "without exemplar support.",
    "Cluster headache":
        "Out-of-pool. High gt_top1 but low ambiguity — should be easy even without "
        "exemplar; tests whether doctor reaches correct dx quickly.",
    "Spontaneous pneumothorax":
        "Out-of-pool. Acute chest pain + dyspnea overlap with PE/ACS; tests whether doctor "
        "screens percussion/breath-sounds features.",
    "Tuberculosis":
        "Out-of-pool. Low gt_top1 in DDXPlus — the differential is hard even with "
        "ground truth. Tests handling of chronic cough + night sweats presentation.",
    "Bronchospasm / acute asthma exacerbation":
        "Out-of-pool. Wheezing + dyspnea overlap with PE; tests respiratory discrimination "
        "without an asthma exemplar.",
    "Guillain-Barré syndrome":
        "Out-of-pool. Rare neurological; tests whether doctor elicits ascending weakness "
        "pattern without relevant exemplar.",
    "Acute pulmonary edema":
        "Out-of-pool. Overlaps with PE and NSTEMI on dyspnea + chest presentation; "
        "tests cardiac vs. fluid-overload differentiation.",
    "Myocarditis":
        "Out-of-pool. Cardiac inflammatory disease overlapping with ACS/pericarditis on chest "
        "pain but requiring different red-flag reasoning (viral prodrome, arrhythmia risk); "
        "tests cardiac differential breadth beyond the ACS spectrum already covered in-pool.",
    "Boerhaave":
        "Out-of-pool. Esophageal rupture — a genuine surgical emergency masquerading as chest "
        "pain; tests whether the doctor screens for a rarer but catastrophic cause of chest "
        "pain with no exemplar support.",
    "Epiglottitis":
        "Out-of-pool. Airway emergency; tests rapid-escalation red-flag recognition for a "
        "fast-progressing presentation with no in-pool analog.",
    "Ebola":
        "Out-of-pool. Highest-severity condition in the DDXPlus taxonomy but rare (n=100 in "
        "the test split); tests handling of a high-consequence infectious presentation with "
        "limited real-world prior exposure.",
    "Larygospasm":
        "Out-of-pool. Airway emergency, highest severity tier; tests acute airway red-flag "
        "screening without exemplar support.",
    "Whooping cough":
        "Out-of-pool. Lower-severity; tests generalization at the less-acute end of the "
        "spectrum, complementing the mostly high-acuity out-of-pool selections.",
    "Croup":
        "Out-of-pool. Pediatric-leaning airway presentation with a comparatively small "
        "sample; tests behavior on a less-common presentation.",
    "Bronchiolitis":
        "Out-of-pool. Smallest class in the entire DDXPlus test split; deliberately included "
        "to test doctor behavior on a genuinely rare presentation.",
    "Myasthenia gravis":
        "Out-of-pool. Neuromuscular weakness presentation, a distinct symptom complex from "
        "Guillain-Barré (already out-of-pool); broadens neurological coverage.",
    "Pancreatic neoplasm":
        "Out-of-pool. Oncologic/abdominal presentation; broadens organ-system coverage beyond "
        "the cardiac/respiratory/neurological focus of the rest of the set.",
    "GERD":
        "Ambiguous. Chest pain + epigastric discomfort closely mimics NSTEMI/angina; "
        "chosen with tight top-2 gap to stress differential ordering.",
    "Anemia":
        "Ambiguous. Fatigue + pallor + dyspnea overlap with multiple conditions; "
        "chosen with tight top-2 gap.",
    "Bronchitis":
        "Ambiguous. Cough + low-grade fever overlaps with pneumonia/URTI; "
        "chosen from patients where gt is NOT top-1 in DDXPlus differential.",
    "PSVT":
        "Ambiguous. Palpitations + chest discomfort overlaps with panic attack and AFib; "
        "chosen with tight top-2 gap.",
    "Stable angina":
        "Ambiguous. Model consistently ranks unstable angina or NSTEMI higher. Canonical "
        "discrimination-test case.",
    "Acute COPD exacerbation / infection":
        "Ambiguous. Overlaps heavily with bronchitis/pneumonia/asthma on cough + dyspnea; "
        "chosen from patients with a tight top-2 differential gap to stress respiratory "
        "discrimination.",
    "Chagas":
        "Ambiguous. Cardiac involvement overlaps with myocarditis/pericarditis; chosen with "
        "a tight top-2 gap to stress cardiac-vs-infectious differentiation.",
    "Scombroid food poisoning":
        "Ambiguous. Histamine-release presentation that can closely mimic anaphylaxis "
        "(already in-pool) on flushing/urticaria/dyspnea; chosen with a tight top-2 gap "
        "specifically to stress that distinction.",
    "Acute rhinosinusitis":
        "Ambiguous. Overlaps with allergic sinusitis/URTI on congestion + facial pain; "
        "chosen with a tight top-2 gap.",
    "Pulmonary neoplasm":
        "Ambiguous. Chronic cough + weight loss overlaps with tuberculosis (already "
        "out-of-pool) and pneumonia; chosen with a tight top-2 gap to stress chronic-vs-acute "
        "respiratory differentiation.",
}


# ---------------------------------------------------------------------------
# Helpers — same logic as loop1/scripts/curate_eval_set.py (read-only, not
# imported from — it's a standalone script, not a package module; these are
# small enough that reimplementing them here is safer than reaching into
# another script's internals)
# ---------------------------------------------------------------------------

def top2_gap(diff_str: str) -> float:
    diff = ast.literal_eval(diff_str)
    return abs(diff[0][1] - diff[1][1]) if len(diff) >= 2 else 1.0


def gt_is_top1(row: pd.Series) -> bool:
    diff = ast.literal_eval(row["DIFFERENTIAL_DIAGNOSIS"])
    return diff[0][0] == row["PATHOLOGY"]


def pick_n(
    df: pd.DataFrame,
    disease: str,
    n: int,
    prefer_gt_top1: bool = True,
    prefer_tight: bool = False,
    rng: random.Random | None = None,
) -> pd.DataFrame:
    """Like the original script's pick_one(), but samples n distinct rows."""
    _rng = rng or random
    sub = df[df["PATHOLOGY"] == disease].copy()
    if sub.empty:
        raise ValueError(f"No patients found for disease: {disease!r}")

    sub["top2_gap"] = sub["DIFFERENTIAL_DIAGNOSIS"].apply(top2_gap)
    sub["gt_top1"] = sub.apply(gt_is_top1, axis=1)

    if prefer_gt_top1:
        filtered = sub[sub["gt_top1"]]
        if len(filtered) >= n:
            sub = filtered

    if prefer_tight:
        tight = sub[sub["top2_gap"] < 0.10]
        if len(tight) >= n:
            sub = tight

    n_avail = min(n, len(sub))
    return sub.sample(n_avail, random_state=_rng.randint(0, 10_000))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    df = pd.read_csv(_DDXPLUS_DIR / "release_test_patients")
    ontology = _load_evidences_ontology()

    rng = random.Random(SEED)
    selected: list[dict] = []

    strata = [
        ("in_pool", IN_POOL, 2, True, False),
        ("out_of_pool", OUT_OF_POOL, 1, True, False),
        ("ambiguous", AMBIGUOUS, 1, False, True),
    ]

    for stratum_name, diseases, n_per_disease, prefer_gt_top1, prefer_tight in strata:
        for disease in diseases:
            rows = pick_n(
                df, disease, n_per_disease,
                prefer_gt_top1=prefer_gt_top1, prefer_tight=prefer_tight, rng=rng,
            )
            for _, row in rows.iterrows():
                patient = _parse_patient_row(row, int(row.name), "test", ontology)
                entry = {
                    "patient_id": patient.patient_id,
                    "row_index": int(row.name),
                    "stratum": stratum_name,
                    "disease": disease,
                    "rationale": RATIONALES[disease],
                    "age": patient.age,
                    "sex": patient.sex,
                    "ground_truth_pathology": patient.ground_truth_pathology,
                    "initial_evidence": patient.initial_evidence,
                    "initial_evidence_code": patient.initial_evidence_code,
                    "gt_is_top1_in_ddxplus": bool(gt_is_top1(row)),
                    "top2_gap": float(top2_gap(row["DIFFERENTIAL_DIAGNOSIS"])),
                    "n_evidences": len(ast.literal_eval(row["EVIDENCES"])),
                    "ground_truth_differential": patient.ground_truth_differential,
                    "symptoms": patient.symptoms,
                    "antecedents": patient.antecedents,
                }
                selected.append(entry)
                print(f"  [{stratum_name:12s}] {disease} -> {patient.patient_id} "
                      f"(gt_top1={entry['gt_is_top1_in_ddxplus']}, gap={entry['top2_gap']:.3f})")

    out_path = _LOOP1_ROOT / "data" / "ddxplus_eval_set.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(selected, f, indent=2)

    print(f"\nSaved {len(selected)} patients to {out_path}")
    print(
        "NOTE: any existing data/admin_portal/red_flag_labels.json now refers to the OLD "
        "eval set's patient_ids and must be regenerated: "
        "python src/admin_portal/eval/labeling/red_flag_labels.py"
    )


if __name__ == "__main__":
    main()
