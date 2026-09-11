"""
Phase 4 bake-off: run turn 0 on all test_cases/ profiles with a fixed seed and
print a summary table showing which exemplars were drawn and what the doctor asked.

Usage:
    uv run python scripts/bake_off.py --seed 42
    uv run python scripts/bake_off.py --seed 42 --cases test_cases/case_02.json test_cases/case_04.json
"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import typer
from rich.console import Console
from rich.table import Table

from loop1.doctor import generate_turn_with_usage
from loop1.schemas import PatientProfile

app = typer.Typer(add_completion=False)
console = Console()

_DEFAULT_CASES_DIR = Path(__file__).parent.parent / "test_cases"


@app.command()
def main(
    seed: int = typer.Option(
        42, "--seed", "-s", help="RNG seed for exemplar selection."
    ),
    cases: list[Path] = typer.Option(
        None,
        "--cases",
        "-c",
        help="Specific case files to run. Defaults to all *.json in test_cases/.",
    ),
    turn_index: int = typer.Option(
        0, "--turn", "-t", help="Turn index to generate (default 0 = opening question)."
    ),
) -> None:
    case_paths = sorted(cases) if cases else sorted(_DEFAULT_CASES_DIR.glob("*.json"))
    if not case_paths:
        console.print("[red]No case files found.[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold]Phase 4 bake-off[/bold]  seed={seed}  turn_index={turn_index}")
    console.print(f"Running {len(case_paths)} case(s)\n")

    table = Table(
        "Case", "Patient", "Exemplars drawn", "Chosen question",
        "Top dx (p)", "Conf", "Tokens",
        show_lines=True,
    )

    for path in case_paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        patient = PatientProfile(**raw)

        # Fresh seeded rng per case so each case's draw is independent but reproducible.
        rng = random.Random(seed)

        console.print(f"[dim]Running {path.name} ...[/dim]")
        result, usage, exemplar_ids = generate_turn_with_usage(
            patient, history=[], turn_index=turn_index, rng=rng
        )

        top_dx = result.current_differential[0] if result.current_differential else None
        top_str = f"{top_dx.dx} ({top_dx.prob:.2f})" if top_dx else "—"

        short_ids = "\n".join(e.replace("ex_", "").replace("_001", "") for e in exemplar_ids)
        patient_label = (
            f"{patient.demographics.age}yo {patient.demographics.sex}\n"
            f"{patient.chief_complaint[:40]}"
        )

        table.add_row(
            path.name,
            patient_label,
            short_ids,
            result.chosen_question,
            top_str,
            f"{result.confidence_to_stop:.2f}",
            str(usage.prompt_tokens),
        )

    console.print(table)

    console.print(
        "\n[dim]Re-run with a different --seed to sample a different exemplar draw. "
        "Full exemplar IDs are in the logs/sessions/ JSONL if you run a full session.[/dim]"
    )


if __name__ == "__main__":
    app()
