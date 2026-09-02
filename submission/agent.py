"""Two-model collaboration engine for Lean 4 autoformalization.

Neither Qwen nor GPT-OSS dominates the other on this benchmark, and the Lean
compiler is a free, perfectly reliable judge, so the design is a portfolio
filtered by that judge rather than a conversation between the models:

1. Answer first. When the statement has a numeric answer slot, both models
   solve the problem in prose and must agree before anyone writes Lean; a wrong
   value makes the theorem false and every later compile error unfixable.
2. Portfolio. Both models draft complete files in parallel, plus hotter samples
   of each, so a problem only one of them can do is still solved.
3. Gate 0. A pure-Python pre-flight rejects candidates that altered the
   problem's declarations or the required numeric-answer form, before the
   compiler is asked about them.
4. Gate 1. The Lean REPL compiles the survivors; the first accepted file wins.
5. Repair. Failures go to the model that did *not* write them, since the two
   fail in different ways. A repair turn that reproduces the same errors ends
   the loop instead of spending the rest of its budget.
6. Sketch, then fill. When whole-file drafting fails, the reasoner writes a
   decomposition of `have ... := by sorry` steps, that structure is compiled on
   its own, and each gap is then closed independently and verified.
7. Restart. The per-problem caps ($1 and eight hours) dwarf one attempt, so a
   failed pass is retried with fresh samples until the budget or clock is out.

Every stage is gated by an environment variable so that each can be switched
off and measured independently; see `experiments.md`. The defaults are the
submission configuration.

Everything here is self-contained; only the public `re_harness` surface and the
Python standard library are used.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from re_harness import (
    AgentResult,
    LLMCallError,
    MODEL_A,
    MODEL_B,
    Problem,
    Services,
)
from re_harness.budget import BudgetAccountingError, BudgetExceeded
from re_harness.lean import numeric_answers_are_literals

REASONER = MODEL_B  # openai/gpt-oss-120b — deep formalization and rewrites
DEBUGGER = MODEL_A  # qwen/qwen3.5-flash-02-23 — fast local error repair

MAX_REPAIRS = 5
MAX_ESCALATIONS = 2

# A reasoning model bills its hidden thinking against `max_tokens`, so a file
# that needs a few thousand tokens still needs an allowance several times that
# to survive the thinking. Undersized allowances do not truncate the answer;
# they consume the whole budget before the answer starts and return nothing.
DRAFT_MAX_TOKENS = 32000
DEBUG_MAX_TOKENS = 24000
ANSWER_MAX_TOKENS = 8000
DRAFT_TEMPERATURE = 0.2
DEBUG_TEMPERATURE = 0.1

# Effort trades thinking against the tokens left for output. Writing a whole
# Lean file needs the output room; naming a numeric answer is a short reply
# whose only cost is the thinking, so it can afford the highest setting.
REASONER_EFFORT = "medium"
ANSWER_EFFORT = "high"
_EFFORT_LADDER = ("high", "medium", "low")

# An empty completion is retried once at lower effort before the stage gives up.
EMPTY_RESPONSE_RETRIES = 1

# Per-problem hard cap is $1.00 (RULES.md). Stop opening new calls with margin
# to spare, since the ledger also reserves a conservative amount per request.
COST_SOFT_CAP_USD = 0.85

# Harness defaults from RULES.md; used only to size the agent's own deadline.
DEFAULT_TIME_LIMIT_S = 28800.0
VERIFY_RESERVE_S = 120.0

MAX_TRACE_ENTRIES = 80

RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_S = 5.0

_DECL_RE = re.compile(
    r"\b(theorem|lemma|abbrev|def|opaque|instance)\s+([A-Za-z_][A-Za-z0-9_'.]*)"
)
_NAT_ABBREV_RE = re.compile(
    r"\babbrev\s+([A-Za-z_][A-Za-z0-9_'.]*)\s*:\s*ℕ\s*:="
)
_LEAN_FENCE_RE = re.compile(r"```(?:lean|lean4)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _lower_effort(effort: str) -> str:
    """The next weaker reasoning setting, leaving more room for the answer."""

    try:
        index = _EFFORT_LADDER.index(effort)
    except ValueError:
        return _EFFORT_LADDER[-1]
    return _EFFORT_LADDER[min(index + 1, len(_EFFORT_LADDER) - 1)]


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _soft_deadline_s() -> float:
    """When to stop opening new work, in seconds from the start of the problem.

    The real cap is the harness's per-problem wall-clock limit, so the agent
    keeps working until most of it is gone rather than stopping after a fixed
    number of attempts. `AGENT_SOFT_DEADLINE_S` overrides this for experiments.
    """

    explicit = _env_float("AGENT_SOFT_DEADLINE_S")
    if explicit is not None:
        return explicit
    limit = _env_float("VM_TIME_LIMIT_S") or DEFAULT_TIME_LIMIT_S
    return max(60.0, limit * 0.85 - VERIFY_RESERVE_S)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class Config:
    """Feature switches, so each phase of the design can be ablated."""

    portfolio_n: int = 4
    smart_repair: bool = True
    answer_first: bool = True
    sketch_fill: bool = True
    max_rounds: int = 2

    @property
    def repair_seeds(self) -> int:
        """How many portfolio candidates get their own repair budget."""

        return 2 if self.smart_repair else 1

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            portfolio_n=_env_int("AGENT_PORTFOLIO_N", 4, minimum=1, maximum=6),
            smart_repair=_env_flag("AGENT_SMART_REPAIR", True),
            answer_first=_env_flag("AGENT_ANSWER_FIRST", True),
            sketch_fill=_env_flag("AGENT_SKETCH_FILL", True),
            max_rounds=_env_int("AGENT_MAX_ROUNDS", 2, minimum=1, maximum=5),
        )


@dataclass(frozen=True)
class _DraftSpec:
    model: str
    temperature: float
    reasoning: bool


# Ordered by expected value: the strongest single draft first, then the other
# model, then hotter samples of each. `portfolio_n` takes a prefix of this
# ladder, so `AGENT_PORTFOLIO_N=1` reproduces a single-drafter scaffold.
_DRAFT_LADDER = (
    _DraftSpec(REASONER, DRAFT_TEMPERATURE, True),
    _DraftSpec(DEBUGGER, 0.3, False),
    _DraftSpec(REASONER, 0.8, True),
    _DraftSpec(DEBUGGER, 0.8, False),
    _DraftSpec(REASONER, 0.5, True),
    _DraftSpec(DEBUGGER, 0.6, False),
)


# --------------------------------------------------------------------------
# Lean source inspection (Gate 0)
# --------------------------------------------------------------------------


def _strip_comments(src: str) -> str:
    """Blank out ``--`` line comments and nestable ``/- -/`` block comments."""

    chars = list(src)
    i = 0
    depth = 0
    line_comment = False
    n = len(src)
    while i < n:
        pair = src[i : i + 2]
        char = src[i]
        if line_comment:
            if char == "\n":
                line_comment = False
            else:
                chars[i] = " "
            i += 1
        elif depth:
            if pair == "/-":
                chars[i : i + 2] = [" ", " "]
                depth += 1
                i += 2
            elif pair == "-/":
                chars[i : i + 2] = [" ", " "]
                depth -= 1
                i += 2
            else:
                if char != "\n":
                    chars[i] = " "
                i += 1
        elif pair == "--":
            chars[i : i + 2] = [" ", " "]
            line_comment = True
            i += 2
        elif pair == "/-":
            chars[i : i + 2] = [" ", " "]
            depth = 1
            i += 2
        else:
            i += 1
    return "".join(chars)


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _decl_headers(src: str) -> dict[str, str]:
    """Map each top-level declaration name to its normalized header.

    The header is the text from the declaration keyword up to the first ``:=``,
    i.e. the name, binders, and stated type/proposition.
    """

    code = _strip_comments(src)
    headers: dict[str, str] = {}
    for match in _DECL_RE.finditer(code):
        name = match.group(2)
        assign = code.find(":=", match.end())
        end = assign if assign != -1 else len(code)
        headers[name] = _normalize(code[match.start() : end])
    return headers


def _numeric_answer_names(challenge: str) -> tuple[str, ...]:
    """Names of ``abbrev NAME : ℕ := ...`` answer slots declared in the challenge."""

    code = _strip_comments(challenge)
    return tuple(match.group(1) for match in _NAT_ABBREV_RE.finditer(code))


def _gate0(challenge: str, solution: str) -> tuple[bool, list[str]]:
    """Cheap pre-flight: statement fidelity + numeric-answer literal policy."""

    errors: list[str] = []
    challenge_headers = _decl_headers(challenge)
    solution_headers = _decl_headers(solution)
    for name, header in challenge_headers.items():
        if name not in solution_headers:
            errors.append(f"declaration `{name}` is missing from the solution")
        elif solution_headers[name] != header:
            errors.append(
                f"declaration `{name}` signature was altered; it must read exactly: {header}"
            )

    ok_literals, literal_errors = numeric_answers_are_literals(
        solution, _numeric_answer_names(challenge)
    )
    if not ok_literals:
        errors.extend(literal_errors)

    return (not errors), errors


def _extract_lean(text: str, fallback: str) -> str:
    """Extract one complete Lean file from an LLM response."""

    fenced = _LEAN_FENCE_RE.findall(text)
    if fenced:
        return fenced[-1].strip() + "\n"
    stripped = text.strip()
    import_at = stripped.find("import ")
    if import_at >= 0:
        return stripped[import_at:].strip() + "\n"
    return fallback


def _format_messages(messages: list[dict[str, Any]], *, limit: int = 6000) -> str:
    chunks: list[str] = []
    for message in messages:
        severity = message.get("severity", "message")
        pos = message.get("pos")
        data = str(message.get("data", "")).strip()
        chunks.append(f"{severity} at {pos}: {data}")
    return "\n\n".join(chunks)[-limit:]


def _error_count(messages: list[dict[str, Any]]) -> int:
    return sum(1 for message in messages if message.get("severity") == "error")


def _sorry_spans(source: str) -> list[tuple[int, int]]:
    """Offsets of every `sorry` token outside comments, in order."""

    code = _strip_comments(source)  # preserves offsets
    return [(match.start(), match.end()) for match in re.finditer(r"\bsorry\b", code)]


def _signature(messages: list[dict[str, Any]]) -> tuple[str, ...]:
    """A position-independent identity for a set of compiler errors.

    Two consecutive repair turns that produce the same signature have made no
    progress, which is the signal to abandon the approach rather than spend the
    remaining turns on it.
    """

    return tuple(
        sorted(
            str(message.get("data", "")).strip().splitlines()[0][:120]
            for message in messages
            if message.get("severity") == "error"
        )
    )


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


_DRAFT_SYSTEM = "\n".join(
    [
        "You are an expert mathematician and Lean 4 (Mathlib) formalizer.",
        "Return one complete Lean 4 file that the compiler accepts, inside a single ```lean code block.",
        "Resolve every `sorry`: prove each theorem, and fill each answer definition"
        " (whether a bare-value `abbrev` or a tactic-term `:= by sorry`).",
        "Preserve the exact theorem/definition names, binders, and statements from the challenge.",
        "For a numeric answer declared as `abbrev name : ℕ := ...`, the body must be a plain"
        " decimal literal (e.g. `76`), never an expression or a tactic block.",
        "Do not use `sorry`, `admit`, `native_decide`, custom `axiom`s, or unsafe escapes.",
    ]
)

_DEBUG_SYSTEM = "\n".join(
    [
        "You are a strict Lean 4 (Mathlib) compiler-error debugger.",
        "Fix only the specific errors reported by the compiler and return the full corrected file",
        " inside a single ```lean code block.",
        "Do NOT rename declarations, change binders, or alter any theorem statement or type.",
        "Do NOT turn a bare-value numeric `abbrev` answer into an expression or a tactic block.",
        "Do not use `sorry`, `admit`, `native_decide`, custom `axiom`s, or unsafe escapes.",
    ]
)

_ESCALATE_SYSTEM = "\n".join(
    [
        "You are an expert Lean 4 (Mathlib) formalizer performing a clean rewrite.",
        "The previous attempt is broken; discard its approach and write a fresh complete file",
        " inside a single ```lean code block.",
        "Prove the EXACT original statement: keep every declaration name, binder, and type unchanged.",
        "For a numeric answer `abbrev name : ℕ := ...`, the body must be a plain decimal literal.",
        "If a suggested numeric answer is stated below and cannot be proved, recompute it yourself.",
        "Do not use `sorry`, `admit`, `native_decide`, custom `axiom`s, or unsafe escapes.",
    ]
)

_ANSWER_SYSTEM = "\n".join(
    [
        "You are an expert competition mathematician. Solve the problem in plain prose;",
        "do NOT write Lean.",
        "Work carefully and state the reasoning that determines the final value.",
        "Finish with one line per requested name, in exactly this form and nothing after it:",
        "ANSWER <name> = <non-negative integer>",
    ]
)

_ADJUDICATE_SYSTEM = "\n".join(
    [
        "You are adjudicating between two candidate answers to a competition problem.",
        "Two solvers disagree. Check both arguments, redo the decisive computation yourself,",
        "and decide which value is correct (or give a third value if both are wrong).",
        "Finish with one line per requested name, in exactly this form and nothing after it:",
        "ANSWER <name> = <non-negative integer>",
    ]
)


_SKETCH_SYSTEM = "\n".join(
    [
        "You are an expert Lean 4 (Mathlib) formalizer writing a proof SKELETON.",
        "Return one complete Lean 4 file inside a single ```lean code block.",
        "Decompose each proof into named intermediate steps:",
        "  have step1 : <precise statement> := by sorry",
        "and finish the theorem from those steps with real tactics, no `sorry`.",
        "Every `sorry` must stand for one intermediate step whose statement is fully written out;",
        "the surrounding structure must elaborate and typecheck on its own.",
        "Aim for 2 to 6 steps, each small enough to prove in a few tactics.",
        "Preserve the exact declaration names, binders, and statements from the challenge, and",
        "give numeric answer definitions a plain decimal literal body (never a `sorry`).",
    ]
)

_FILL_SYSTEM = "\n".join(
    [
        "You are an expert Lean 4 (Mathlib) prover closing a single goal.",
        "The file below contains exactly one placeholder, «FILL_ME», in tactic position.",
        "Return ONLY the tactic text that replaces «FILL_ME», inside a single ```lean code block.",
        "Write it on ONE line; sequence tactics with `;` or `<;>` if you need several.",
        "Do not restate the file, the `have`, or the `:= by`. Never use `sorry` or `native_decide`.",
    ]
)


def _problem_block(problem: Problem, hint: str = "") -> str:
    block = [
        f"Problem id: {problem.id}",
        "",
        "Problem description:",
        problem.description,
        "",
        "Challenge Lean file (statements to preserve, with `sorry` placeholders):",
        "```lean",
        problem.challenge,
        "```",
    ]
    if hint:
        block.extend(["", hint])
    return "\n".join(block)


def _answer_messages(problem: Problem, names: tuple[str, ...]) -> list[dict[str, str]]:
    user = "\n".join(
        [
            f"Problem id: {problem.id}",
            "",
            problem.description,
            "",
            "The formal statement fixes these answer slots:",
            "```lean",
            problem.challenge,
            "```",
            "",
            "Report a value for: " + ", ".join(names),
        ]
    )
    return [
        {"role": "system", "content": _ANSWER_SYSTEM},
        {"role": "user", "content": user},
    ]


def _adjudicate_messages(
    problem: Problem, names: tuple[str, ...], proposals: list[tuple[str, str]]
) -> list[dict[str, str]]:
    sections = [
        f"Problem id: {problem.id}",
        "",
        problem.description,
        "",
        "Report a value for: " + ", ".join(names),
    ]
    for index, (_model, reasoning) in enumerate(proposals, start=1):
        sections.extend([
            "",
            f"Solver {index} wrote:",
            "```text",
            reasoning[-6000:],
            "```",
        ])
    return [
        {"role": "system", "content": _ADJUDICATE_SYSTEM},
        {"role": "user", "content": "\n".join(sections)},
    ]


def _sketch_messages(problem: Problem, hint: str, feedback: str) -> list[dict[str, str]]:
    sections = [_problem_block(problem, hint)]
    if feedback:
        sections.extend([
            "",
            "The previous skeleton did not elaborate. Fix the structure:",
            "```text",
            feedback,
            "```",
        ])
    return [
        {"role": "system", "content": _SKETCH_SYSTEM},
        {"role": "user", "content": "\n".join(sections)},
    ]


def _fill_messages(problem: Problem, marked_source: str) -> list[dict[str, str]]:
    user = "\n".join(
        [
            f"Problem id: {problem.id}",
            "",
            problem.description,
            "",
            "Proof skeleton with the goal to close marked «FILL_ME»:",
            "```lean",
            marked_source,
            "```",
        ]
    )
    return [
        {"role": "system", "content": _FILL_SYSTEM},
        {"role": "user", "content": user},
    ]


def _draft_messages(problem: Problem, hint: str = "") -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _DRAFT_SYSTEM},
        {"role": "user", "content": _problem_block(problem, hint)},
    ]


def _debug_messages(
    problem: Problem, candidate: str, feedback: str, hint: str = ""
) -> list[dict[str, str]]:
    user = "\n".join(
        [
            _problem_block(problem, hint),
            "",
            "Current candidate that failed to compile:",
            "```lean",
            candidate,
            "```",
            "",
            "Lean compiler feedback:",
            "```text",
            feedback,
            "```",
        ]
    )
    return [
        {"role": "system", "content": _DEBUG_SYSTEM},
        {"role": "user", "content": user},
    ]


def _escalate_messages(
    problem: Problem, candidate: str, reason: str, hint: str = ""
) -> list[dict[str, str]]:
    user = "\n".join(
        [
            _problem_block(problem, hint),
            "",
            "The previous attempt (below) could not be repaired:",
            "```lean",
            candidate,
            "```",
            "",
            "Why it failed:",
            "```text",
            reason,
            "```",
        ]
    )
    return [
        {"role": "system", "content": _ESCALATE_SYSTEM},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------
# Session: budgeted, logged access to the two models and the Lean REPL
# --------------------------------------------------------------------------


@dataclass
class _Candidate:
    """A complete Lean file plus everything known about why it fails."""

    source: str
    author: str
    accepted: bool
    gate0_ok: bool
    errors: int
    feedback: str
    signature: tuple[str, ...] = ()

    @property
    def rank(self) -> tuple[int, int]:
        """Sort key: compilable-shaped candidates first, then fewest errors."""

        return (0 if self.gate0_ok else 1, self.errors)


class _Session:
    """Per-problem state: budget guards, best-so-far, and a compact trace."""

    def __init__(self, problem: Problem, services: Services, config: Config) -> None:
        self.problem = problem
        self.services = services
        self.config = config
        self.start = time.monotonic()
        self.soft_deadline_s = _soft_deadline_s()
        self.rounds = 0
        self.spent_usd = 0.0
        self.halted = False
        self.calls = 0
        self.empty_responses = 0
        self.truncated_responses = 0
        self.models: set[str] = set()
        self.trace: list[dict[str, Any]] = []
        self.best_accepted: str | None = None
        self.best_gate0: str | None = None
        self.best_errors = 10**9
        self.answers: dict[str, str] = {}
        self.answer_agreement = "not_applicable"

    @property
    def hint(self) -> str:
        """Answer values to pin into every formalization prompt, if settled."""

        if not self.answers:
            return ""
        values = ", ".join(f"{name} = {value}" for name, value in self.answers.items())
        confidence = (
            "Both solvers independently agree on"
            if self.answer_agreement == "agreed"
            else "After adjudication the best available value is"
        )
        return (
            f"{confidence} the following numeric answer(s): {values}."
            " Use exactly these decimal literals in the answer definitions."
        )

    # -- guards -----------------------------------------------------------

    @property
    def exhausted(self) -> bool:
        if self.halted:
            return True
        if self.spent_usd >= COST_SOFT_CAP_USD:
            return True
        return (time.monotonic() - self.start) >= self.soft_deadline_s

    def note(self, stage: str, **fields: Any) -> None:
        if len(self.trace) < MAX_TRACE_ENTRIES:
            self.trace.append({"stage": stage, **fields})

    # -- model and compiler access ---------------------------------------

    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        reasoning: bool = False,
        effort: str | None = None,
        seed: int | None = None,
    ) -> str | None:
        """One budgeted model call. Returns None once the session is spent.

        A reasoning model that spends its whole allowance thinking returns an
        empty completion. At the call site that is indistinguishable from a
        refusal, so the stage skips the model and the failure never reaches the
        trace. Record it instead, and retry once with weaker reasoning so the
        allowance goes to the answer.
        """

        current_effort = effort or REASONER_EFFORT
        for retry in range(EMPTY_RESPONSE_RETRIES + 1):
            response = await self._request(
                model,
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                effort=current_effort if reasoning else None,
                seed=seed,
            )
            if response is None:
                return None

            if response.finish_reason == "length":
                self.truncated_responses += 1
            content = response.content or ""
            if content.strip():
                return content

            self.empty_responses += 1
            self.note(
                "empty_response",
                model=model,
                effort=current_effort if reasoning else None,
                finish=response.finish_reason,
                retry=retry,
            )
            if retry == EMPTY_RESPONSE_RETRIES or self.exhausted:
                return None
            current_effort = _lower_effort(current_effort)
        return None

    async def _request(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        effort: str | None,
        seed: int | None,
    ) -> Any | None:
        """The budgeted request itself, retried past recoverable rate limits."""

        if self.exhausted:
            self.halted = True
            return None

        kwargs: dict[str, Any] = {}
        if effort is not None:
            kwargs["reasoning"] = {"effort": effort}
        if seed is not None:
            kwargs["seed"] = seed

        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                response = await self.services.llm.complete(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **kwargs,
                )
            except (BudgetExceeded, BudgetAccountingError):
                self.halted = True
                return None
            except LLMCallError as exc:
                # The harness only leaves the ledger open when the provider
                # refused before generating and reported no cost, so that is
                # the one failure this pipeline may safely retry.
                retryable = "reported no cost" in str(exc)
                if not retryable or attempt == RATE_LIMIT_RETRIES or self.exhausted:
                    self.halted = True
                    return None
                self.note("rate_limited", model=model, attempt=attempt + 1)
                await asyncio.sleep(RATE_LIMIT_BACKOFF_S * (attempt + 1))
                continue

            self.calls += 1
            self.models.add(model)
            self.spent_usd += float(response.usage.get("cost") or 0.0)
            return response
        return None

    async def compile(self, source: str) -> Any | None:
        """One Lean REPL check. Returns None once the session is spent."""

        if self.exhausted:
            self.halted = True
            return None
        return await self.services.lean.check_file(source)

    async def evaluate(self, source: str, author: str) -> _Candidate | None:
        """Run Gate 0 then the Lean REPL. Returns None if the session is spent."""

        ok, errors = _gate0(self.problem.challenge, source)
        if not ok:
            return _Candidate(
                source=source,
                author=author,
                accepted=False,
                gate0_ok=False,
                errors=len(errors),
                feedback="Signature/answer policy violation: " + "; ".join(errors),
                signature=tuple(sorted(errors)),
            )

        check = await self.compile(source)
        if check is None:
            return None
        if check.accepted:
            candidate = _Candidate(source, author, True, True, 0, "")
            self.record(candidate)
            return candidate

        candidate = _Candidate(
            source=source,
            author=author,
            accepted=False,
            gate0_ok=True,
            errors=_error_count(check.messages),
            feedback=_format_messages(check.messages) or "The candidate did not compile.",
            signature=_signature(check.messages),
        )
        self.record(candidate)
        return candidate

    def record(self, candidate: _Candidate) -> None:
        """Keep the best fallback and checkpoint whenever it improves."""

        if candidate.accepted:
            self.best_accepted = candidate.source
            self.best_errors = 0
            self.services.checkpoint(candidate.source, {"stage": "solved"})
            return
        # An unfinished file scores zero however few errors it has, so it must
        # never displace a genuine near-miss as the fallback answer.
        if _sorry_spans(candidate.source):
            return
        if candidate.gate0_ok and candidate.errors < self.best_errors:
            self.best_errors = candidate.errors
            self.best_gate0 = candidate.source
            self.services.checkpoint(
                candidate.source, {"stage": "best_so_far", "errors": candidate.errors}
            )


# --------------------------------------------------------------------------
# Pipeline stages
# --------------------------------------------------------------------------


_ANSWER_LINE_RE = re.compile(
    r"^[\s`*]*ANSWER[\s:]+([A-Za-z_][A-Za-z0-9_'.]*)\s*=\s*([0-9]+)[\s`*.]*$",
    re.MULTILINE,
)


def _parse_answers(text: str, names: tuple[str, ...]) -> dict[str, str]:
    """Last stated value for each requested name, if the model reported one."""

    found = {
        match.group(1): match.group(2).lstrip("0") or "0"
        for match in _ANSWER_LINE_RE.finditer(text)
    }
    return {name: found[name] for name in names if name in found}


async def _agree_on_answers(session: _Session) -> None:
    """Settle the numeric answers before anyone writes Lean.

    A wrong value makes the theorem false, so every downstream compile error is
    unfixable. Both models solve the problem in prose first; when they agree the
    answer is pinned for the formalization stage, and when they disagree each
    sees the other's argument once before the reasoner's verdict is taken.
    """

    names = _numeric_answer_names(session.problem.challenge)
    if not names or not session.config.answer_first:
        return

    messages = _answer_messages(session.problem, names)
    replies = await asyncio.gather(
        session.complete(
            REASONER,
            messages,
            max_tokens=ANSWER_MAX_TOKENS,
            temperature=0.2,
            reasoning=True,
            effort=ANSWER_EFFORT,
        ),
        session.complete(
            DEBUGGER, messages, max_tokens=ANSWER_MAX_TOKENS, temperature=0.2
        ),
    )
    proposals = [
        (model, text) for model, text in zip((REASONER, DEBUGGER), replies) if text
    ]
    parsed = [(model, _parse_answers(text, names)) for model, text in proposals]
    complete = [(model, answers) for model, answers in parsed if len(answers) == len(names)]

    if len(complete) == 2 and complete[0][1] == complete[1][1]:
        session.answers = complete[0][1]
        session.answer_agreement = "agreed"
    elif complete:
        verdicts = await asyncio.gather(
            *(
                session.complete(
                    model,
                    _adjudicate_messages(session.problem, names, proposals),
                    max_tokens=ANSWER_MAX_TOKENS,
                    temperature=0.2,
                    reasoning=model == REASONER,
                    effort=ANSWER_EFFORT,
                )
                for model in (REASONER, DEBUGGER)
            )
        )
        adjudicated = [
            _parse_answers(text, names) for text in verdicts if text
        ]
        final = next(
            (answers for answers in adjudicated if len(answers) == len(names)),
            complete[0][1],
        )
        session.answers = final
        session.answer_agreement = "adjudicated"
    else:
        session.answer_agreement = "unavailable"

    session.note(
        "answers", agreement=session.answer_agreement, values=dict(session.answers)
    )


async def _draft_portfolio(session: _Session, round_index: int) -> list[_Candidate]:
    """Draft with both models in parallel, then compile the distinct results.

    Neither model dominates the other on this benchmark, so a portfolio of
    independent drafts captures problems that only one of them can solve.
    """

    specs = _DRAFT_LADDER[: session.config.portfolio_n]
    messages = _draft_messages(session.problem, session.hint)
    drafts = await asyncio.gather(
        *(
            session.complete(
                spec.model,
                messages,
                max_tokens=DRAFT_MAX_TOKENS,
                temperature=spec.temperature,
                reasoning=spec.reasoning,
                seed=None if round_index == 0 and index == 0 else 1000 * round_index + index,
            )
            for index, spec in enumerate(specs)
        )
    )

    # Identical drafts are common at low temperature; compiling one is enough.
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()
    unusable = 0
    for spec, text in zip(specs, drafts):
        source = _extract_lean(text, "") if text else ""
        if not source.strip():
            unusable += 1
            continue
        key = _normalize(source)
        if key in seen:
            continue
        seen.add(key)
        sources.append((source, spec.model))
    session.note(
        "portfolio",
        round=round_index,
        drafted=len(sources),
        requested=len(specs),
        unusable=unusable,
    )

    candidates: list[_Candidate] = []
    for source, author in sources:
        candidate = await session.evaluate(source, author)
        if candidate is None:
            break
        session.note(
            "draft",
            model=author,
            gate0=candidate.gate0_ok,
            errors=candidate.errors,
            accepted=candidate.accepted,
        )
        candidates.append(candidate)
        if candidate.accepted:
            break

    candidates.sort(key=lambda candidate: candidate.rank)
    return candidates


def _debugger_for(session: _Session, candidate: _Candidate) -> str:
    """Hand a broken file to the model that did not write it.

    The two models fail differently: the reasoner writes Lean that parses but
    does not typecheck (hallucinated Mathlib lemmas, mismatched types), while
    Qwen writes well-typed Lean with weak tactic choices. Each is therefore a
    better critic of the other's output than of its own.
    """

    if not session.config.smart_repair:
        return DEBUGGER
    return DEBUGGER if candidate.author == REASONER else REASONER


async def _repair_once(
    session: _Session, candidate: _Candidate, debugger: str
) -> _Candidate | None:
    """One debugging turn against the compiler's complaint."""

    text = await session.complete(
        debugger,
        _debug_messages(session.problem, candidate.source, candidate.feedback, session.hint),
        max_tokens=DEBUG_MAX_TOKENS,
        temperature=DEBUG_TEMPERATURE,
        reasoning=debugger == REASONER,
    )
    if text is None:
        return None
    source = _extract_lean(text, candidate.source)
    return await session.evaluate(source, debugger)


async def _rewrite(session: _Session, candidate: _Candidate) -> _Candidate | None:
    """Discard a stalled approach and ask the reasoner for a fresh proof."""

    text = await session.complete(
        REASONER,
        _escalate_messages(
            session.problem, candidate.source, candidate.feedback, session.hint
        ),
        max_tokens=DRAFT_MAX_TOKENS,
        temperature=DRAFT_TEMPERATURE,
        reasoning=True,
    )
    if text is None:
        return None
    source = _extract_lean(text, candidate.source)
    return await session.evaluate(source, REASONER)


MAX_SKETCH_ATTEMPTS = 2
MAX_GAPS = 12
FILL_MARKER = "«FILL_ME»"
# The reply is one tactic line, but the reasoner still thinks first, so the
# allowance has to cover the thinking rather than the tactic.
FILL_MAX_TOKENS = 8000


def _extract_tactic(text: str) -> str | None:
    """Pull a single-line tactic out of a fill response."""

    fenced = _LEAN_FENCE_RE.findall(text)
    body = fenced[-1] if fenced else text
    for line in body.strip().splitlines():
        candidate = line.strip().strip("`").strip()
        if not candidate or candidate.startswith("--"):
            continue
        lowered = candidate.lower()
        if any(word in lowered for word in ("sorry", "native_decide", "import ", "axiom ")):
            return None
        if candidate.startswith(("theorem ", "lemma ", "have ", ":=")):
            return None
        return candidate
    return None


async def _write_skeleton(session: _Session) -> str | None:
    """Get a decomposition whose structure elaborates, gaps aside.

    Compiling the skeleton before any gap is proved separates two failures that
    a monolithic draft conflates: a decomposition that does not typecheck, and
    an individual step that is hard to prove.
    """

    feedback = ""
    for attempt in range(1, MAX_SKETCH_ATTEMPTS + 1):
        text = await session.complete(
            REASONER,
            _sketch_messages(session.problem, session.hint, feedback),
            max_tokens=DRAFT_MAX_TOKENS,
            temperature=DRAFT_TEMPERATURE,
            reasoning=True,
        )
        if text is None:
            session.note("sketch", attempt=attempt, outcome="no_response")
            return None
        source = _extract_lean(text, "")
        if not source.strip():
            session.note("sketch", attempt=attempt, outcome="no_lean_block")
            feedback = (
                "Your previous reply contained no Lean code block."
                " Reply with one complete Lean file and nothing else."
            )
            continue

        ok, errors = _gate0(session.problem.challenge, source)
        if not ok:
            feedback = "Signature/answer policy violation: " + "; ".join(errors)
            session.note("sketch", attempt=attempt, gate0=False)
            continue

        check = await session.compile(source)
        if check is None:
            return None
        gaps = len(_sorry_spans(source))
        structural_errors = _error_count(check.messages)
        session.note("sketch", attempt=attempt, gate0=True, gaps=gaps, errors=structural_errors)
        if check.accepted:
            session.record(_Candidate(source, REASONER, True, True, 0, ""))
            return source
        if structural_errors == 0 and 0 < gaps <= MAX_GAPS:
            return source
        feedback = _format_messages(check.messages) or "The skeleton did not elaborate."
    return None


async def _fill_gaps(session: _Session, skeleton: str) -> bool:
    """Close each `sorry` in the skeleton independently, cheapest model first.

    A fill is kept only when the compiler reports no errors for the whole file,
    so a bad suggestion is reverted rather than corrupting the skeleton.
    """

    source = skeleton
    index = 0
    while not session.exhausted:
        spans = _sorry_spans(source)
        if not spans:
            break
        if index >= len(spans):
            break
        start, end = spans[index]
        marked = source[:start] + FILL_MARKER + source[end:]

        filled = False
        for model in (DEBUGGER, REASONER):
            text = await session.complete(
                model,
                _fill_messages(session.problem, marked),
                max_tokens=FILL_MAX_TOKENS,
                temperature=0.1,
                reasoning=model == REASONER,
            )
            if text is None:
                return False
            tactic = _extract_tactic(text)
            if tactic is None:
                continue
            trial = source[:start] + tactic + source[end:]
            if not _gate0(session.problem.challenge, trial)[0]:
                continue
            check = await session.compile(trial)
            if check is None:
                return False
            if check.accepted:
                session.note("fill", gap=index, model=model, outcome="solved")
                session.record(_Candidate(trial, model, True, True, 0, ""))
                return True
            if _error_count(check.messages) == 0:
                session.note("fill", gap=index, model=model, outcome="closed")
                source = trial
                filled = True
                break
            session.note("fill", gap=index, model=model, outcome="rejected")

        if not filled:
            index += 1
    return False


async def _sketch_and_fill(session: _Session) -> bool:
    """Prove a decomposition step by step when whole-file drafting has failed."""

    if not session.config.sketch_fill:
        return False
    if session.exhausted:
        session.note("sketch", outcome="skipped_exhausted")
        return False
    skeleton = await _write_skeleton(session)
    if skeleton is None:
        return False
    if session.best_accepted is not None:
        return True
    return await _fill_gaps(session, skeleton)


async def _attempt(session: _Session, round_index: int) -> bool:
    """One full pass: draft a portfolio, repair the best of it, then sketch."""

    candidates = await _draft_portfolio(session, round_index)
    for candidate in candidates[: session.config.repair_seeds]:
        if candidate.accepted:
            return True
        if session.exhausted:
            return session.best_accepted is not None
        if await _pursue(session, candidate):
            return True
    return await _sketch_and_fill(session)


async def _pursue(session: _Session, candidate: _Candidate) -> bool:
    """Repair a candidate, escalating to a clean rewrite when repair stalls."""

    escalations = 0
    while not session.exhausted:
        previous_signature = candidate.signature
        for attempt in range(1, MAX_REPAIRS + 1):
            debugger = _debugger_for(session, candidate)
            repaired = await _repair_once(session, candidate, debugger)
            if repaired is None:
                return False
            session.note(
                "repair",
                attempt=attempt,
                model=debugger,
                gate0=repaired.gate0_ok,
                errors=repaired.errors,
            )
            # A candidate that violates Gate 0 is not a usable base for the
            # next turn; keep the last valid source and report the violation.
            candidate = (
                repaired
                if repaired.gate0_ok
                else _Candidate(
                    candidate.source,
                    candidate.author,
                    False,
                    True,
                    candidate.errors,
                    repaired.feedback,
                    repaired.signature,
                )
            )
            if repaired.accepted:
                return True
            if session.config.smart_repair and repaired.signature == previous_signature:
                session.note("stalled", attempt=attempt, errors=repaired.errors)
                break
            previous_signature = repaired.signature

        if escalations >= MAX_ESCALATIONS:
            return False
        escalations += 1
        rewritten = await _rewrite(session, candidate)
        if rewritten is None:
            return False
        session.note(
            "escalation", n=escalations, gate0=rewritten.gate0_ok, errors=rewritten.errors
        )
        if rewritten.accepted:
            return True
        candidate = rewritten
    return False


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------


class SubmissionAgent:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.from_env()

    async def solve(self, problem: Problem, services: Services) -> AgentResult:
        session = _Session(problem, services, self.config)

        await _agree_on_answers(session)

        # Each round is a fresh attempt at the whole pipeline with new samples.
        # Restarting is worthwhile because the per-problem caps ($1 and eight
        # hours) are far larger than one attempt costs.
        for round_index in range(self.config.max_rounds):
            if session.exhausted:
                break
            session.rounds = round_index + 1
            if await _attempt(session, round_index) or session.best_accepted is not None:
                break

        return self._result(session)

    def _result(self, session: _Session) -> AgentResult:
        solution = session.best_accepted or session.best_gate0 or session.problem.challenge
        stage = "solved" if session.best_accepted else "fallback"
        session.services.checkpoint(solution, {"stage": stage})
        return AgentResult(
            solution,
            {
                "stage": stage,
                "rounds": session.rounds,
                "llm_calls": session.calls,
                "empty_responses": session.empty_responses,
                "truncated_responses": session.truncated_responses,
                "approx_spent_usd": round(session.spent_usd, 6),
                "models_used": sorted(session.models),
                "halted_early": session.halted,
                "portfolio_n": self.config.portfolio_n,
                "smart_repair": self.config.smart_repair,
                "sketch_fill": self.config.sketch_fill,
                "max_rounds": self.config.max_rounds,
                "answer_agreement": session.answer_agreement,
                "answers": dict(session.answers),
                "trace": session.trace,
            },
        )


def create_agent() -> SubmissionAgent:
    return SubmissionAgent()
