#!/usr/bin/env python3
"""Print a per-problem table for one run directory.

Usage: summarize_run.py <run-dir>

The run directory is the one containing ``summary.json`` (for example
``outputs/dev8/submission/20260901T000000Z``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _stage(run_dir: Path, problem_id: str) -> str:
    """Best-effort label for where the agent stopped on this problem."""

    result_path = run_dir / problem_id / "result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "-"
    metadata = result.get("agent_metadata") or {}
    parts = [str(metadata.get("stage", "-"))]
    for key in ("rounds", "llm_calls"):
        if key in metadata:
            parts.append(f"{key}={metadata[key]}")
    if metadata.get("answer_agreement"):
        parts.append(f"answers={metadata['answer_agreement']}")
    return " ".join(parts)


def _row(values: list[str], widths: list[int]) -> str:
    return "  ".join(value.ljust(width) for value, width in zip(values, widths)).rstrip()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    run_dir = Path(argv[1])
    summary_path = run_dir / "summary.json"
    try:
        summary: dict[str, Any] = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {summary_path}: {exc}", file=sys.stderr)
        return 1

    header = ["problem", "result", "status", "cost_usd", "wall_s", "agent stage"]
    rows = []
    for problem in summary.get("problems", []):
        rows.append([
            str(problem.get("problem_id", "?")),
            "PASS" if problem.get("passed") else "fail",
            str(problem.get("status", "?")),
            f"{float(problem.get('actual_cost_usd') or 0.0):.4f}",
            f"{float(problem.get('wall_s') or 0.0):.0f}",
            _stage(run_dir, str(problem.get("problem_id", ""))),
        ])

    widths = [max(len(header[i]), *(len(row[i]) for row in rows)) if rows else len(header[i])
              for i in range(len(header))]
    print(_row(header, widths))
    print(_row(["-" * width for width in widths], widths))
    for row in rows:
        print(_row(row, widths))

    total = summary.get("total_points", 0)
    maximum = summary.get("max_points", len(rows))
    cost = float(summary.get("actual_cost_usd") or 0.0)
    wall = float(summary.get("wall_s") or 0.0)
    print()
    print(f"score {total}/{maximum}   spend ${cost:.4f}   wall {wall:.0f}s   dir {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
