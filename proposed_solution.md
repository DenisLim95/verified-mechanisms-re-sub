# Architecture Blueprint: The Multi-Gate Collaboration Engine

This document describes how the two fixed models coordinate to solve and formally
verify the Lean 4 problems, and how validation is staged from cheapest to most
expensive to respect the per-problem cap ($1 and 8 hours of wall-clock) and the
$50 hard budget cap across the whole run.

## 1. Model Roles & Responsibilities

### GPT-OSS-120B (`openai/gpt-oss-120b`) — The Mathematical Reasoner
- **Role:** Deconstruct the natural-language problem, generate informal
  reasoning, and draft the initial formal proof or answer term.
- **Trigger:** Invoked at the start of every problem, and again on structural
  escalations when the local debug loop cannot recover.

### Qwen 3.5 Flash (`qwen/qwen3.5-flash-02-23`) — The Syntax & Refinement Agent
- **Role:** Iteratively patch syntax errors, type mismatches, and local
  compilation errors reported by the Lean kernel.
- **Trigger:** Invoked inside the rapid local error-correction loop.

## 2. The Verification Pipeline (Gate Sequence)

Validation is ordered cheapest/fastest to most expensive so that budget and
wall-clock time are spent only when a candidate is worth compiling.

```
                          [START]
                             │
                 GPT-OSS-120B drafts solution
                             │
                             ▼
  ┌────────►  GATE 0: Python signature & literal check
  │                          │
  │                ┌─────────┴─────────┐
  │              [FAIL]              [PASS]
  │                │                   │
  │                │                   ▼
  │                │     GATE 1: local `check_file` compile
  │                │          ┌────────┴────────┐
  │                │        [FAIL]            [PASS]
  │                │          │                 │
  │                │   Max Qwen retries?        ▼
  │                │      ┌────┴────┐   [SUCCESS: return solution]
  │                │    [NO]      [YES]
  │                │      │         │
  │                │      ▼         ▼
  │                │  Qwen 3.5   GPT-OSS-120B
  │                │  debugs     escalation rewrite
  │                │      │         │
  └────────────────┴──────┴─────────┘
```

### Gate 0: Python Signature & Literal Guard (pre-flight)
Runs before any compilation, so a malformed candidate is rejected without
spending compile time.
- **Multi-signature guard:** Scan `challenge.lean` and the agent's solution for
  every top-level declaration (`theorem`, `lemma`, `abbrev`, `def`) up to `:=`,
  confirming the model did not alter variable types, signatures, or declaration
  names.
- **Literal-policy guard:** For the bare-value "find-the-value" problems
  (`p06`, `p07`, `p10`), confirm the `abbrev` answer body resolves to a plain
  decimal literal (`^\s*\d+\s*$`) rather than an algebraic expression or a
  tactic block.
- **Outcome:** On failure, skip heavy compilation entirely and route straight to
  a structural rewrite.

### Gate 1: Local `check_file` Compilation
- **Checks:** Compiles the file through the local Lean REPL (`services.lean`) and
  confirms zero remaining `sorry` macros — covering both proof-section `sorry`
  blocks and the Putnam-style tactic-term `abbrev := by sorry` blocks.
- **Outcome:** On pass, the candidate is ready for submission. On failure
  (syntax errors or unproven components), route to the Qwen debug loop.

> Note: the agent only has `check_file` (a warm, untrusted REPL). The trusted
> `leanprover/comparator` is reserved for the harness's final grade, so Gate 0's
> signature guard is what protects us from silently proving an altered statement.

## 3. Detailed Data Payloads per Step

| Phase | Actor | Input Payload | System Directive |
| --- | --- | --- | --- |
| 1. Initial drafting | GPT-OSS-120B | `problem.md` (English) + `challenge.lean` (signatures + mixed `sorry` placeholders) | Deconstruct the problem, resolve all `sorry` placeholders (theorems, tactic blocks, or bare values), and strictly preserve declaration signatures. |
| 2. Gate 0 guard | Python script | `challenge.lean` + agent solution | Compare extracted signatures and check decimal-literal formatting; reject immediately if invalid. |
| 3. Gate 1 compile | Local Lean kernel | Agent solution | Run the local compiler to verify syntax validity and `sorry`-free execution. |
| 4. Syntax loop | Qwen 3.5 Flash | Original `challenge.lean` + current failing solution + Lean `stdout`/`stderr` | Fix the local syntax/tactic error. Do not alter top-level signatures or turn bare-value answers into expressions. |
| 5. Escalation | GPT-OSS-120B | `problem.md` + `challenge.lean` + failed solution + gate error trace (signature mismatch or persistent compile failure) | Discard the broken path and perform a clean architectural rewrite of the proof or answer term. |

## 4. Stopping Rule & Failure Handling

Because GPT-OSS escalations are the most expensive action against the $1
per-problem cap, the loop is bounded on two independent axes and always emits a
best-effort candidate rather than crashing.

- **Qwen retries (inner loop):** Cap at 5 debug attempts per draft. On exhaustion,
  hand control back to GPT-OSS as an escalation.
- **GPT-OSS escalations (outer loop):** Cap at 2–3 total structural rewrites per
  problem (a Gate 0 failure and a maxed-out Qwen loop draw from the *same*
  escalation budget). Once exhausted, stop drafting.
- **Budget/time guard:** Before every model call, check the running cost and the
  wall-clock deadline; if either is close to its cap, halt the loop immediately
  and skip remaining escalations.
- **Fallback on halt:** When the loop stops for any reason (escalations spent,
  budget/time guard, or an unrecoverable error), submit the last candidate that
  passed Gate 1; if none ever compiled, submit the last Gate-0-valid draft; if
  even that is missing, submit the original `challenge.lean` unchanged so the run
  always produces a well-formed file.

Gate 0 itself catches structural defects (renamed/retyped declarations, non-literal
answers) but cannot detect a proof that is vacuously true under a weakened
hypothesis — that residual risk is only resolved by the harness's final
comparator, so escalations should re-check the full statement, not just the
signature text.

## 5. Implementation Checklist for `submission/agent.py`

- **Robust regex extraction:** Parse `theorem`, `lemma`, `abbrev`, and `def`
  blocks dynamically, stripping comments and normalizing whitespace before
  comparing signatures.
- **Stateful loop management:** Cap Qwen syntax-debug attempts (e.g. 5 retries)
  before forcing an escalation back to GPT-OSS.
- **Token & cost tracking:** Log cumulative API usage per problem to stay under
  the $1 per-problem cap and safely within the $50 hard budget across all
  problems.
