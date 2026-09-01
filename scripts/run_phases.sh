#!/usr/bin/env bash
# Run the dev8 evaluation for every phase of the ladder in experiments.md and
# record where each run landed, so the comparison table can be regenerated.
#
#   bash scripts/run_phases.sh          # all phases, 0 through 5
#   bash scripts/run_phases.sh 0 1      # only these phases
#   N_WORKERS=2 bash scripts/run_phases.sh
#
# Concurrency is held fixed across phases on purpose: it affects provider rate
# limiting, so varying it between phases would confound the comparison.
set -uo pipefail
cd "$(dirname "$0")/.."
test -x .venv/bin/python || { echo "Run bash scripts/setup.sh first." >&2; exit 1; }

OUT_ROOT=${DEV8_OUT_ROOT:-outputs/dev8}
LEDGER="$OUT_ROOT/phase-ledger.tsv"
LOG_DIR="$OUT_ROOT/logs"
export N_WORKERS=${N_WORKERS:-1}

mkdir -p "$LOG_DIR"
[ -f "$LEDGER" ] || printf 'phase\trun_dir\tstarted_at\texit_code\n' > "$LEDGER"

# Each phase turns off every feature introduced after it.
phase_env() {
  case "$1" in
    0) echo "AGENT_PORTFOLIO_N=1 AGENT_SMART_REPAIR=0 AGENT_ANSWER_FIRST=0 AGENT_SKETCH_FILL=0 AGENT_MAX_ROUNDS=1" ;;
    1) echo "AGENT_SMART_REPAIR=0 AGENT_ANSWER_FIRST=0 AGENT_SKETCH_FILL=0 AGENT_MAX_ROUNDS=1" ;;
    2) echo "AGENT_ANSWER_FIRST=0 AGENT_SKETCH_FILL=0 AGENT_MAX_ROUNDS=1" ;;
    3) echo "AGENT_SKETCH_FILL=0 AGENT_MAX_ROUNDS=1" ;;
    4) echo "AGENT_MAX_ROUNDS=1" ;;
    5) echo "" ;;
    *) return 1 ;;
  esac
}

phases=("$@")
[ ${#phases[@]} -eq 0 ] && phases=(0 1 2 3 4 5)

for phase in "${phases[@]}"; do
  settings=$(phase_env "$phase") || { echo "unknown phase: $phase" >&2; exit 2; }
  started=$(date -u +%Y%m%dT%H%M%SZ)
  log="$LOG_DIR/phase${phase}-${started}.log"

  echo
  echo "=============================================================="
  echo "phase $phase  |  ${settings:-all defaults}  |  N_WORKERS=$N_WORKERS"
  echo "log: $log"
  echo "=============================================================="

  # `env -u` clears any AGENT_* left exported in this shell, so a phase is
  # defined only by the variables listed above.
  env -u AGENT_PORTFOLIO_N -u AGENT_SMART_REPAIR -u AGENT_ANSWER_FIRST \
      -u AGENT_SKETCH_FILL -u AGENT_MAX_ROUNDS -u AGENT_SOFT_DEADLINE_S \
      $settings bash scripts/run_dev8.sh 2>&1 | tee "$log"
  code=${PIPESTATUS[0]}

  run_dir=$(sed -n 's/^out \([^;]*\);.*/\1/p' "$log" | tail -n 1)
  printf '%s\t%s\t%s\t%s\n' "$phase" "${run_dir:-unknown}" "$started" "$code" >> "$LEDGER"

  if [ "$code" -ne 0 ]; then
    echo "phase $phase exited $code; continuing with the next phase" >&2
  fi
done

echo
.venv/bin/python scripts/phase_report.py
bash scripts/bundle_results.sh
