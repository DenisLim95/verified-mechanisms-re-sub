#!/usr/bin/env bash
# Run the dev8 evaluation for every phase of the ladder in experiments.md and
# record where each run landed, so the comparison table can be regenerated.
#
#   bash scripts/run_phases.sh          # all phases, 0 through 5
#   bash scripts/run_phases.sh 0 1      # only these phases
#   REPEATS=3 bash scripts/run_phases.sh 2 3
#   N_WORKERS=2 bash scripts/run_phases.sh
#
# Concurrency is held fixed across phases on purpose: it affects provider rate
# limiting, so varying it between phases would confound the comparison.
#
# Eight problems decided by sampling cannot separate a three-point score from a
# four-point one, so `REPEATS` runs each phase more than once and the report
# compares solve rates instead of single outcomes. Repeats are interleaved
# rather than consecutive so that a change in provider behaviour partway
# through the ladder spreads across phases instead of landing on one of them.
set -uo pipefail
cd "$(dirname "$0")/.."
test -x .venv/bin/python || { echo "Run bash scripts/setup.sh first." >&2; exit 1; }

OUT_ROOT=${DEV8_OUT_ROOT:-outputs/dev8}
LEDGER="$OUT_ROOT/phase-ledger.tsv"
LOG_DIR="$OUT_ROOT/logs"
export N_WORKERS=${N_WORKERS:-1}
REPEATS=${REPEATS:-1}

mkdir -p "$LOG_DIR"
[ -f "$LEDGER" ] || printf 'phase\trepeat\trun_dir\tstarted_at\texit_code\n' > "$LEDGER"

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
  phase_env "$phase" >/dev/null || { echo "unknown phase: $phase" >&2; exit 2; }
done

for repeat in $(seq 1 "$REPEATS"); do
for phase in "${phases[@]}"; do
  settings=$(phase_env "$phase")
  started=$(date -u +%Y%m%dT%H%M%SZ)
  log="$LOG_DIR/phase${phase}-r${repeat}-${started}.log"

  echo
  echo "=============================================================="
  echo "phase $phase  repeat $repeat/$REPEATS  |  ${settings:-all defaults}"
  echo "N_WORKERS=$N_WORKERS  |  deadline=${AGENT_SOFT_DEADLINE_S:-default}s"
  echo "log: $log"
  echo "=============================================================="

  # `env -u` clears the feature switches left exported in this shell, so a
  # phase is defined only by the variables listed above. AGENT_SOFT_DEADLINE_S
  # is deliberately not cleared: it bounds how long the agent works on one
  # problem, and holding it fixed across every phase is what makes a ladder
  # fit in an evening without changing what is being compared.
  env -u AGENT_PORTFOLIO_N -u AGENT_SMART_REPAIR -u AGENT_ANSWER_FIRST \
      -u AGENT_SKETCH_FILL -u AGENT_MAX_ROUNDS \
      $settings bash scripts/run_dev8.sh 2>&1 | tee "$log"
  code=${PIPESTATUS[0]}

  run_dir=$(sed -n 's/^out \([^;]*\);.*/\1/p' "$log" | tail -n 1)
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$phase" "$repeat" "${run_dir:-unknown}" "$started" "$code" >> "$LEDGER"

  if [ "$code" -ne 0 ]; then
    echo "phase $phase repeat $repeat exited $code; continuing" >&2
  fi
done
done

echo
.venv/bin/python scripts/phase_report.py
bash scripts/bundle_results.sh
