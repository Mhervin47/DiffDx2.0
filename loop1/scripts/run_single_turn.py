"""Run generate_turn() on a single patient profile and print the result."""
import json
import random
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import typer

from loop1.doctor import generate_turn_with_usage
from loop1.retrieval import get_exemplar_by_id
from loop1.schemas import PatientProfile

app = typer.Typer(add_completion=False)


@app.command()
def main(
    profile: Path = typer.Option(
        ..., "--profile", "-p", help="Path to a patient profile JSON file."
    ),
    turn_index: int = typer.Option(
        0, "--turn", "-t", help="Turn index to pass to generate_turn()."
    ),
    seed: Optional[int] = typer.Option(
        None, "--seed", "-s", help="Seed for exemplar selection (makes draw reproducible)."
    ),
    force_exemplar: Optional[list[str]] = typer.Option(
        None,
        "--force-exemplar",
        "-e",
        help=(
            "Force specific exemplar IDs into the prompt (bypasses random selection). "
            "Repeat the flag for each exemplar: -e ex_cvst_001 -e ex_pericarditis_001 ..."
        ),
    ),
) -> None:
    if not profile.exists():
        typer.echo(f"Error: profile file not found: {profile}", err=True)
        raise typer.Exit(1)

    raw = json.loads(profile.read_text(encoding="utf-8"))
    patient = PatientProfile(**raw)

    forced = None
    if force_exemplar:
        forced = [get_exemplar_by_id(eid) for eid in force_exemplar]

    rng = random.Random(seed) if (seed is not None and forced is None) else None

    typer.echo(f"Profile  : {profile}")
    typer.echo(f"Patient  : {patient.demographics.age}yo {patient.demographics.sex} — {patient.chief_complaint}")
    if forced:
        typer.echo(f"Exemplars: FORCED — {[ex.exemplar_id for ex in forced]}")
    else:
        typer.echo(f"Seed     : {seed if seed is not None else 'unseeded'}")
    typer.echo(f"Calling  : generate_turn(turn_index={turn_index}) ...\n")

    result, usage, exemplar_ids = generate_turn_with_usage(
        patient, history=[], turn_index=turn_index, rng=rng, forced_exemplars=forced
    )

    typer.echo(f"Exemplars drawn : {exemplar_ids}")
    typer.echo(
        f"Tokens          : {usage.prompt_tokens} prompt"
        f"{f', {usage.completion_tokens} completion' if usage.completion_tokens is not None else ''}"
        f" (model_actual={usage.model_actual})\n"
    )
    typer.echo(result.model_dump_json(indent=2))


if __name__ == "__main__":
    app()
