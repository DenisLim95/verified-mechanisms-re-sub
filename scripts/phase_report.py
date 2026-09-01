#!/usr/bin/env python3
"""Build the phase comparison tables from the run ledger.

Usage: phase_report.py [ledger-path]

Prints Markdown ready to paste into the Results section of experiments.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_LEDGER = Path("outputs/dev8/phase-ledger.tsv")
MANIFEST = Path("sample-problems-dev8/manifest.json")


def latest_runs(ledger: Path) -> dict[str, Path]:
    """Most recent run directory per phase; a rerun supersedes its predecessor."""

    runs: dict[str, Path] = {}
    for line in ledger.read_text(encoding="utf-8").splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) < 2 or fields[1] == "unknown":
            continue
        runs[fields[0]] = Path(fields[1])
    return runs


def problem_order() -> list[str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [entry["id"] for entry in manifest["problems"]]


def main(argv: list[str]) -> int:
    ledger = Path(argv[1]) if len(argv) > 1 else DEFAULT_LEDGER
    if not ledger.is_file():
        print(f"no ledger at {ledger}; run scripts/run_phases.sh first", file=sys.stderr)
        return 1

    runs = latest_runs(ledger)
    summaries: dict[str, dict] = {}
    for phase, run_dir in sorted(runs.items()):
        try:
            summaries[phase] = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skipping phase {phase}: {exc}", file=sys.stderr)

    if not summaries:
        print("no readable summaries", file=sys.stderr)
        return 1

    phases = sorted(summaries)

    print("| Phase | Score | Spend | Wall | Run |")
    print("| --- | --- | --- | --- | --- |")
    for phase in phases:
        summary = summaries[phase]
        print(
            f"| {phase} | {summary['total_points']}/{summary['max_points']} "
            f"| ${float(summary['actual_cost_usd']):.4f} "
            f"| {float(summary['wall_s']) / 60:.0f} min "
            f"| `{runs[phase].name}` |"
        )

    outcomes = {
        phase: {
            problem["problem_id"]: bool(problem["passed"])
            for problem in summaries[phase]["problems"]
        }
        for phase in phases
    }

    print()
    print("| Problem | " + " | ".join(phases) + " |")
    print("| --- | " + " | ".join("---" for _ in phases) + " |")
    for problem in problem_order():
        cells = ["P" if outcomes[phase].get(problem) else "." for phase in phases]
        print(f"| `{problem}` | " + " | ".join(cells) + " |")

    print()
    for phase in phases:
        previous = outcomes.get(str(int(phase) - 1))
        if previous is None:
            continue
        gained = [p for p in problem_order() if outcomes[phase].get(p) and not previous.get(p)]
        lost = [p for p in problem_order() if not outcomes[phase].get(p) and previous.get(p)]
        if gained or lost:
            print(f"phase {phase}: gained {', '.join(gained) or 'none'}; "
                  f"regressed {', '.join(lost) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
