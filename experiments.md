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
bash scripts/run_dev8.sh
```

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

Score is out of 8. Fill in one row per phase run.

| Phase | Score | Spend | Notes |
| --- | --- | --- | --- |
| 0 | | | |
| 1 | | | |
| 2 | | | |
| 3 | | | |
| 4 | | | |
| 5 | | | |

### Per-problem outcomes

Totals hide which problems moved, so record the per-problem column for each
phase. `P` = passed, `.` = failed.

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

### Reference: solo baselines on these 8 problems

From `vm-outputs/outputs/baseline/`, run under the older 20-minute cap:
GPT-OSS solo 2/8, Qwen solo 2/8, union 4/8.
