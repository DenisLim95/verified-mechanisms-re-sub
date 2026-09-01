#!/usr/bin/env bash
# Run the submission agent on the 8-problem development set and print a
# per-problem table. Extra arguments are forwarded to run.py, so phase
# configuration is supplied as environment variables:
#
#   AGENT_PORTFOLIO_N=1 AGENT_SMART_REPAIR=0 AGENT_ANSWER_FIRST=0 \
#   AGENT_SKETCH_FILL=0 AGENT_MAX_ROUNDS=1 bash scripts/run_dev8.sh
#
# See experiments.md for the phase ladder.
set -euo pipefail
cd "$(dirname "$0")/.."
test -x .venv/bin/python || { echo "Run bash scripts/setup.sh first." >&2; exit 1; }

OUT_ROOT=${DEV8_OUT_ROOT:-outputs/dev8}

log=$(mktemp "${TMPDIR:-/tmp}/re-takehome-dev8.XXXXXX")
trap 'rm -f "$log"' EXIT

.venv/bin/python run.py \
  --problems sample-problems-dev8 \
  --out "$OUT_ROOT" \
  "$@" | tee "$log"

run_dir=$(sed -n 's/^out \([^;]*\);.*/\1/p' "$log" | tail -n 1)
if [ -z "$run_dir" ]; then
  echo "Could not determine the run directory from run.py output." >&2
  exit 1
fi

echo
.venv/bin/python scripts/summarize_run.py "$run_dir"
