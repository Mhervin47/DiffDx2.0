"""
Phase 6 item 5 — Confidence plateau fix validation.

Runs scripted sessions on 3 cases with pre-canned patient answers.
Pass condition (per phase6_notes.md): termination fires on confidence
(not max-turns) on at least 3 of 5 base cases.

Usage:
    python scripts/validate_confidence.py
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from loop1.config import config
from loop1.schemas import PatientProfile
from loop1.session import Session


# ---------------------------------------------------------------------------
# Scripted answers per case
# These confirm the leading diagnosis progressively. The session terminates
# as soon as confidence >= confidence_to_stop (currently 0.55).
# ---------------------------------------------------------------------------

SCRIPTED_ANSWERS: dict[str, list[str]] = {
    "case_01_acs": [
        # The profile already has: crushing chest pain + dyspnea + diaphoresis + nausea +
        # hypertension + T2DM + hyperlipidemia + father MI + ex-smoker.
        # Doctor will likely ask about radiation, severity, prior episodes, etc.
        "The pain goes down my left arm and up into my jaw.",
        "No, this has never happened before, not like this.",
        "The pain started when I was sitting at my desk, not exercising.",
        "Yes I feel very sick and light-headed.",
        "No leg swelling, no recent travel.",
    ],
    "case_02_cvst": [
        # Profile: 34F, headache + dizziness + blurred vision, OCP, history of migraine.
        # Rewritten to be question-agnostic: each answer front-loads a distinct CVST
        # discriminator so the model receives the clue regardless of what it asks.
        # Key discriminators: positional headache (ICP), OCP+smoking (thrombosis risk),
        # transient visual obscurations, absence of infection/stroke signs, pattern
        # different from prior migraines, pulsatile tinnitus.
        "The headache is constant and dull — it's definitely worse when I lie down and gets a bit better when I sit up. That's different from my usual migraines.",
        "I've been on the combined pill for about 4 years and I do smoke, about 10 cigarettes a day. No nausea with this headache.",
        "My vision goes blurry for a few seconds at a time, especially when I bend forward or strain — it's happened about five times today.",
        "No fever, no stiff neck, no new weakness in my limbs or difficulty speaking. I've never had a blood clot before.",
        "This headache has been building over five days and getting worse, not coming and going like my normal migraines. No flashing lights or zigzag patterns before it.",
        "I've noticed a low whooshing sound in my ears and a feeling of pressure inside my head.",
    ],
    "case_06_measles": [
        # Profile: 4yo M, unvaccinated, international travel 10 days ago, daycare,
        # fever + cough + runny nose + red eyes + rash (face → trunk).
        # Doctor will confirm: Koplik spots, exposure, vaccination, rash spread.
        "He has not had the MMR vaccine at all.",
        "Three other children at the daycare were diagnosed with measles last week.",
        "There are small white spots on the inside of his cheeks, the doctor at the walk-in noticed them.",
        "The rash started behind his ears and on his face, now it's on his chest too.",
        "No, no other new symptoms.",
    ],
}


def _load_profile(path: Path) -> PatientProfile:
    data = json.loads(path.read_text())
    data["session_id"] = str(uuid.uuid4())  # fresh ID to avoid log collision
    return PatientProfile(**data)


def run_scripted_session(
    case_name: str,
    profile: PatientProfile,
    answers: list[str],
) -> dict:
    """
    Run a session using pre-canned answers instead of interactive Prompt.ask.
    Returns a summary dict with termination_reason and confidence trajectory.
    """
    answer_iter = iter(answers)
    confidence_trajectory: list[float] = []

    original_generate = None

    def capturing_generate(p, h, turn_index, **kwargs):
        out, usage, ids = original_generate(p, h, turn_index, **kwargs)
        confidence_trajectory.append(out.confidence_to_stop)
        return out, usage, ids

    import loop1.session as session_module
    original_generate = session_module.generate_turn_with_usage

    def scripted_answer(prompt_text):
        try:
            return next(answer_iter)
        except StopIteration:
            return "I don't know."

    # Patch closing_turn so we don't need a second LLM call just for validation
    def no_closing(profile, differential, model=None):
        return None

    # Patch retrieval to avoid loading sentence_transformers (not under test here)
    import loop1.doctor as doctor_module
    import loop1.session as _sess_mod
    from loop1.schemas import Exemplar
    _dummy_exemplar = Exemplar(
        exemplar_id="ex_999",
        tags=["validation"],
        context_summary="validation stub",
        good_next_question="How long have you had this symptom?",
        rationale="stub for confidence validation",
        frozen=False,
    )

    def no_retrieval(profile, n=3, lambda_=0.7):
        return [_dummy_exemplar] * n

    # Patch profile updater to avoid hitting the 6000 TPM rate limit on
    # llama-3.1-8b-instant — profile updates are irrelevant to confidence testing.
    # extract_profile_delta now returns (ProfileDelta, LlmUsage) — this stub
    # matches that 2-tuple shape; the delta itself ({}) is unchanged.
    def no_delta(patient_answer, profile, model=None):
        return {}, None

    def no_apply(profile, delta):
        return profile

    with (
        patch.object(session_module, "generate_turn_with_usage", side_effect=capturing_generate),
        patch("loop1.session.generate_closing_turn", side_effect=no_closing),
        patch("loop1.session.Prompt.ask", side_effect=scripted_answer),
        patch.object(doctor_module, "get_exemplars_for_profile", side_effect=no_retrieval),
        patch.object(_sess_mod, "extract_profile_delta", side_effect=no_delta),
        patch.object(_sess_mod, "apply_delta", side_effect=no_apply),
    ):
        session = Session(profile=profile, max_turns=8)
        record = session.run()

    return {
        "case": case_name,
        "termination_reason": record.termination_reason,
        "turns_taken": len(record.turn_history),
        "confidence_trajectory": confidence_trajectory,
        "final_confidence": confidence_trajectory[-1] if confidence_trajectory else None,
        "primary_diagnosis": record.primary_diagnosis,
    }


def main() -> None:
    test_case_dir = Path(__file__).parent.parent / "test_cases"
    threshold = config["thresholds"]["confidence_to_stop"]

    cases = [
        ("case_01_acs", test_case_dir / "case_01.json", SCRIPTED_ANSWERS["case_01_acs"]),
        ("case_02_cvst", test_case_dir / "case_02.json", SCRIPTED_ANSWERS["case_02_cvst"]),
        ("case_06_measles", test_case_dir / "case_06.json", SCRIPTED_ANSWERS["case_06_measles"]),
    ]

    print(f"\nConfidence Plateau Validation — threshold = {threshold}")
    print(f"Model: {config['models']['doctor']}")
    print("=" * 60)

    results = []
    for case_name, profile_path, answers in cases:
        print(f"\nRunning {case_name}...")
        profile = _load_profile(profile_path)
        try:
            result = run_scripted_session(case_name, profile, answers)
            results.append(result)
            traj = " → ".join(f"{c:.2f}" for c in result["confidence_trajectory"])
            status = "✓ CONFIDENCE" if result["termination_reason"] == "confidence_threshold" else "✗ MAX-TURNS"
            print(f"  {status}  turns={result['turns_taken']}  trajectory: {traj}")
            print(f"  Primary: {result['primary_diagnosis']}")
        except Exception as exc:
            print(f"  ERROR: {exc}")
            import traceback; traceback.print_exc()
            results.append({"case": case_name, "termination_reason": "error", "error": str(exc)})

    print("\n" + "=" * 60)
    print("Results summary:")
    confidence_stops = sum(
        1 for r in results if r.get("termination_reason") == "confidence_threshold"
    )
    for r in results:
        reason = r.get("termination_reason", "error")
        mark = "✓" if reason == "confidence_threshold" else "✗"
        print(f"  {mark} {r['case']}: {reason}")

    print(f"\n  {confidence_stops}/{len(results)} terminated on confidence (need >= 3 of 5 to pass)")
    if confidence_stops >= 3:
        print("  PASS — threshold 0.55 fix validated.")
    else:
        print("  FAIL — model still not reaching 0.55 on most cases.")
        print("  Consider: Hypothesis A prompt fix (align calibration scale to 0.55).")


if __name__ == "__main__":
    main()
