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


def latest_runs(ledger: Path) -> dict[str, list[Path]]:
    """Run directories per phase, in repeat order.

    Runs are keyed by (phase, repeat) so that redoing a botched run replaces it
    rather than being counted as an extra sample, while genuine repeats of the
    same phase accumulate.
    """

    lines = ledger.read_text(encoding="utf-8").splitlines()
    if not lines:
        return {}
    columns = {name: index for index, name in enumerate(lines[0].split("\t"))}
    phase_at = columns.get("phase", 0)
    repeat_at = columns.get("repeat")
    dir_at = columns.get("run_dir", 1)

    seen: dict[tuple[str, str], Path] = {}
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) <= max(phase_at, dir_at) or fields[dir_at] == "unknown":
            continue
        repeat = fields[repeat_at] if repeat_at is not None and len(fields) > repeat_at else "1"
        seen[(fields[phase_at], repeat)] = Path(fields[dir_at])

    runs: dict[str, list[Path]] = {}
    for (phase, _), run_dir in sorted(seen.items()):
        runs.setdefault(phase, []).append(run_dir)
    return runs


def problem_order() -> list[str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [entry["id"] for entry in manifest["problems"]]


def health(run_dir: Path) -> dict[str, int]:
    """Whether the scaffold actually ran, as opposed to how well it scored.

    A model whose completions come back empty is skipped by every stage, so a
    phase can score badly because its feature never executed rather than
    because the feature does not work. That is invisible in the score table.
    """

    totals = {"calls": 0, "empty": 0, "truncated": 0, "sketch": 0, "unusable": 0}
    for result_path in sorted(run_dir.glob("*/result.json")):
        try:
            metadata = json.loads(result_path.read_text(encoding="utf-8"))["agent_metadata"]
        except (OSError, json.JSONDecodeError, KeyError):
            continue
        totals["calls"] += metadata.get("llm_calls", 0)
        totals["empty"] += metadata.get("empty_responses", 0)
        totals["truncated"] += metadata.get("truncated_responses", 0)
        for entry in metadata.get("trace") or []:
            if entry.get("stage") == "sketch":
                totals["sketch"] += 1
            totals["unusable"] += entry.get("unusable", 0)
    return totals


def main(argv: list[str]) -> int:
    ledger = Path(argv[1]) if len(argv) > 1 else DEFAULT_LEDGER
    if not ledger.is_file():
        print(f"no ledger at {ledger}; run scripts/run_phases.sh first", file=sys.stderr)
        return 1

    runs = latest_runs(ledger)
    summaries: dict[str, list[dict]] = {}
    for phase, run_dirs in sorted(runs.items()):
        for run_dir in run_dirs:
            try:
                text = (run_dir / "summary.json").read_text(encoding="utf-8")
            except OSError as exc:
                print(f"skipping {run_dir}: {exc}", file=sys.stderr)
                continue
            try:
                summaries.setdefault(phase, []).append(json.loads(text))
            except json.JSONDecodeError as exc:
                print(f"skipping {run_dir}: {exc}", file=sys.stderr)

    if not summaries:
        print("no readable summaries", file=sys.stderr)
        return 1

    phases = sorted(summaries)

    print("| Phase | Runs | Score | Spend | Wall | Run ids |")
    print("| --- | --- | --- | --- | --- | --- |")
    for phase in phases:
        group = summaries[phase]
        points = [summary["total_points"] for summary in group]
        maximum = group[0]["max_points"]
        mean = sum(points) / len(points)
        score = f"{mean:.1f}/{maximum}"
        if len(points) > 1:
            score += f" ({min(points)}\u2013{max(points)})"
        print(
            f"| {phase} | {len(group)} | {score} "
            f"| ${sum(float(s['actual_cost_usd']) for s in group):.4f} "
            f"| {sum(float(s['wall_s']) for s in group) / 60:.0f} min "
            f"| " + ", ".join(f"`{d.name}`" for d in runs[phase]) + " |"
        )

    print()
    print("| Phase | Calls | Empty | Truncated | Unusable drafts | Sketch turns |")
    print("| --- | --- | --- | --- | --- | --- |")
    for phase in phases:
        totals = {key: 0 for key in ("calls", "empty", "truncated", "sketch", "unusable")}
        for run_dir in runs[phase]:
            for key, value in health(run_dir).items():
                totals[key] += value
        share = 100 * totals["empty"] / totals["calls"] if totals["calls"] else 0.0
        print(
            f"| {phase} | {totals['calls']} | {totals['empty']} ({share:.0f}%) "
            f"| {totals['truncated']} | {totals['unusable']} | {totals['sketch']} |"
        )

    # How often each problem was solved, out of the repeats of that phase. With
    # one repeat this is the familiar pass/fail cell.
    solves = {
        phase: {
            problem["problem_id"]: sum(
                bool(entry["passed"])
                for summary in summaries[phase]
                for entry in summary["problems"]
                if entry["problem_id"] == problem["problem_id"]
            )
            for problem in summaries[phase][0]["problems"]
        }
        for phase in phases
    }

    print()
    print("| Problem | " + " | ".join(phases) + " |")
    print("| --- | " + " | ".join("---" for _ in phases) + " |")
    for problem in problem_order():
        cells = []
        for phase in phases:
            passes = solves[phase].get(problem, 0)
            total = len(summaries[phase])
            cells.append(f"{passes}/{total}" if total > 1 else ("P" if passes else "."))
        print(f"| `{problem}` | " + " | ".join(cells) + " |")

    print()
    for phase in phases:
        previous = str(int(phase) - 1)
        if previous not in solves:
            continue
        here, before = len(summaries[phase]), len(summaries[previous])
        gained, lost = [], []
        for problem in problem_order():
            delta = solves[phase].get(problem, 0) / here - solves[previous].get(problem, 0) / before
            (gained if delta > 0 else lost if delta < 0 else []).append(problem)
        if gained or lost:
            print(f"phase {phase}: gained {', '.join(gained) or 'none'}; "
                  f"regressed {', '.join(lost) or 'none'}")

    if all(len(summaries[phase]) == 1 for phase in phases):
        print()
        print("Single run per phase: differences of one or two problems are not")
        print("separable from sampling noise. Use REPEATS=3 to compare solve rates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
