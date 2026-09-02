# Experiments log

Every phase of the scaffold is evaluated on the same 8-problem development set
so that a change in score is attributable to the change in the agent.

## Development set (`sample-problems-dev8/`)

Chosen from the published baseline runs in `vm-outputs/` to maximise signal:

| Problem | GPT-OSS solo | Qwen solo | Why it is in the set |
| --- | --- | --- | --- |
| `p05_gcd_mersenne` | pass | fail | only one model solves it |
| `p06_pow_mod` | fail | pass | only one model solves it |
| `p08_sum_products` | fail | pass | only one model solves it |
| `putnam_2020_a2` | pass | fail | only one model solves it |
| `p09_imo1964` | fail | fail | beyond either model alone |
| `p10_factorial_pow` | fail | fail | beyond either model alone |
| `rmo_2000_2` | fail | fail | beyond either model alone |
| `rmo_2001_2` | fail | fail | beyond either model alone |

The first four measure whether the scaffold captures the *union* of what the two
models can already do alone. The last four measure whether collaboration reaches
past that union, which is the interesting claim for part two.

Deliberately excluded: `p01`–`p04`, which both models solve alone and which
therefore cannot discriminate between scaffolds.

## How to run a phase

```bash
bash scripts/run_dev8.sh          # one phase, current configuration
REPEATS=3 bash scripts/run_phases.sh   # the whole ladder, three samples each
```

Eight problems scored once cannot separate a 3 from a 4: in the first ladder
six of the eight problems flipped at least once across phases. Compare solve
rates over `REPEATS` samples rather than single outcomes.

The agent's features are cumulative and each is gated by an environment
variable, so a phase is reproduced by turning the later features off. Nothing
needs to be checked out or reverted.

| Phase | Feature added | Command |
| --- | --- | --- |
| 0 | none (single GPT-OSS draft, Qwen-only repair) | `AGENT_PORTFOLIO_N=1 AGENT_SMART_REPAIR=0 AGENT_ANSWER_FIRST=0 AGENT_SKETCH_FILL=0 AGENT_MAX_ROUNDS=1 bash scripts/run_dev8.sh` |
| 1 | parallel draft portfolio | `AGENT_SMART_REPAIR=0 AGENT_ANSWER_FIRST=0 AGENT_SKETCH_FILL=0 AGENT_MAX_ROUNDS=1 bash scripts/run_dev8.sh` |
| 2 | cross-model repair, no-progress escalation, two repair seeds | `AGENT_ANSWER_FIRST=0 AGENT_SKETCH_FILL=0 AGENT_MAX_ROUNDS=1 bash scripts/run_dev8.sh` |
| 3 | answer-first agreement on numeric answers | `AGENT_SKETCH_FILL=0 AGENT_MAX_ROUNDS=1 bash scripts/run_dev8.sh` |
| 4 | sketch-then-fill | `AGENT_MAX_ROUNDS=1 bash scripts/run_dev8.sh` |
| 5 | budget-aware restart rounds (all defaults) | `bash scripts/run_dev8.sh` |

Defaults with no environment variables set are the submission configuration
(`AGENT_PORTFOLIO_N=4 AGENT_SMART_REPAIR=1 AGENT_ANSWER_FIRST=1
AGENT_SKETCH_FILL=1 AGENT_MAX_ROUNDS=2`), which is what the judges run.

Runs are written to `outputs/dev8/submission/<timestamp>/`. To re-print the
table for an earlier run:

```bash
.venv/bin/python scripts/summarize_run.py outputs/dev8/submission/<timestamp>
```

Practical notes:

- Final grading runs the Comparator in a fresh container per problem, which
  costs a couple of minutes on its own. A dev8 run therefore has a floor of
  roughly twenty minutes even when every problem is solved instantly. Use
  `--n-workers 2` if there is memory for it (about 5 GB per worker).
- Leave `VM_TIME_LIMIT_S` at its default. Lowering it to shorten a run makes
  problems fail as `timed_out` during grading rather than during solving.

## Results

Regenerate with `.venv/bin/python scripts/phase_report.py`.

### Ladder 1 (2026-09-01/02) — void, kept as a record

| Phase | Score | Spend | Wall |
| --- | --- | --- | --- |
| 0 | 1/8 | $0.0959 | 88 min |
| 1 | 3/8 | $0.2942 | 153 min |
| 2 | 4/8 | $0.3231 | 199 min |
| 3 | 4/8 | $0.2805 | 187 min |
| 4 | 3/8 | $0.3574 | 291 min |
| 5 | 4/8 | $0.6557 | 700 min |

| Problem | 0 | 1 | 2 | 3 | 4 | 5 |
| --- | --- | --- | --- | --- | --- | --- |
| `p05_gcd_mersenne` | . | P | P | . | . | P |
| `p06_pow_mod` | P | . | P | . | P | P |
| `p08_sum_products` | . | P | P | P | P | P |
| `putnam_2020_a2` | . | . | P | P | P | . |
| `p09_imo1964` | . | . | . | P | . | . |
| `p10_factorial_pow` | . | P | . | P | . | P |
| `rmo_2000_2` | . | . | . | . | . | . |
| `rmo_2001_2` | . | . | . | . | . | . |

This ladder does not measure what it was supposed to measure, for two reasons.

**The reasoner was absent.** GPT-OSS returned an empty completion on 74–82% of
its calls in every phase, always with `finish_reason: length`: at
`max_tokens=16000` and `effort=high` it spent the whole allowance thinking and
never began the Lean file. Every stage treated the empty reply as "this model
declined" and moved on, so the runs are close to Qwen working alone. Answer-first
is the exception and did work, because naming a number is a short reply that fits.

The clearest casualty is phase 4: across the whole phase the `sketch` stage
appears in exactly one problem's trace, and in phase 5 not at all, because
`_write_skeleton` asks GPT-OSS first and returned early every time. Phase 4's
3/8 is not a measurement of sketch-then-fill.

**One sample cannot resolve one point.** Six of the eight problems flip at
least once across the ladder, the union of all phases is 6/8, and no single
phase exceeds 4/8. Phases 1 through 5 (3, 4, 4, 3, 4) are indistinguishable.

Fixes: `DRAFT_MAX_TOKENS` 16000 → 32000, `DEBUG_MAX_TOKENS` 12000 → 24000,
`FILL_MAX_TOKENS` 3000 → 8000, `REASONER_EFFORT` high → medium with the answer
stage pinned to high, an empty completion now retried once at lower effort and
recorded as an `empty_response` trace entry, and a scaffold-health table in
`phase_report.py` so a stage that never ran is visible next to the score.

### Ladder 2 — after the fixes

| Phase | Runs | Score | Spend | Notes |
| --- | --- | --- | --- | --- |
| 0 | | | | |
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |
| 4 | | | | |
| 5 | | | | |

Cells below are solves out of repeats.

| Problem | 0 | 1 | 2 | 3 | 4 | 5 |
| --- | --- | --- | --- | --- | --- | --- |
| `p05_gcd_mersenne` | | | | | | |
| `p06_pow_mod` | | | | | | |
| `p08_sum_products` | | | | | | |
| `putnam_2020_a2` | | | | | | |
| `p09_imo1964` | | | | | | |
| `p10_factorial_pow` | | | | | | |
| `rmo_2000_2` | | | | | | |
| `rmo_2001_2` | | | | | | |

Check the health table first: if `Empty` is not near zero, the scores below are
measuring the same absence again.

### Reference: solo baselines on these 8 problems

From `vm-outputs/outputs/baseline/`, run under the older 20-minute cap:
GPT-OSS solo 2/8, Qwen solo 2/8, union 4/8.
