"""Multi-Gate Collaboration Engine.

GPT-OSS (the reasoner) drafts a complete Lean file; a pure-Python pre-flight
(Gate 0) rejects candidates that altered the problem's declarations or the
required numeric-answer form; the local Lean REPL (Gate 1) compiles what
survives; Qwen (the debugger) patches compile errors in a bounded loop; and
GPT-OSS performs bounded structural rewrites when the debugger cannot recover.

Everything here is self-contained; only the public `re_harness` surface and the
Python standard library are used. See `proposed_solution.md` for the design.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
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

MAX_QWEN_RETRIES = 5
MAX_ESCALATIONS = 2

DRAFT_MAX_TOKENS = 16000
DEBUG_MAX_TOKENS = 12000
DRAFT_TEMPERATURE = 0.2
DEBUG_TEMPERATURE = 0.1
REASONER_EFFORT = "high"

# Per-problem hard cap is $1.00 (RULES.md). Stop opening new calls with margin
# to spare, since the ledger also reserves a conservative amount per request.
COST_SOFT_CAP_USD = 0.85

_DECL_RE = re.compile(
    r"\b(theorem|lemma|abbrev|def|opaque|instance)\s+([A-Za-z_][A-Za-z0-9_'.]*)"
)
_NAT_ABBREV_RE = re.compile(
    r"\babbrev\s+([A-Za-z_][A-Za-z0-9_'.]*)\s*:\s*ℕ\s*:="
)
_LEAN_FENCE_RE = re.compile(r"```(?:lean|lean4)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


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
        "Do not use `sorry`, `admit`, `native_decide`, custom `axiom`s, or unsafe escapes.",
    ]
)


def _problem_block(problem: Problem) -> str:
    return "\n".join(
        [
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
    )


def _draft_messages(problem: Problem) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _DRAFT_SYSTEM},
        {"role": "user", "content": _problem_block(problem)},
    ]


def _debug_messages(problem: Problem, candidate: str, feedback: str) -> list[dict[str, str]]:
    user = "\n".join(
        [
            _problem_block(problem),
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


def _escalate_messages(problem: Problem, candidate: str, reason: str) -> list[dict[str, str]]:
    user = "\n".join(
        [
            _problem_block(problem),
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


def _soft_deadline_s() -> float | None:
    raw = os.environ.get("AGENT_SOFT_DEADLINE_S", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


@dataclass
class _State:
    spent_usd: float = 0.0
    halted: bool = False
    calls: int = 0
    models: set[str] = field(default_factory=set)


class SubmissionAgent:
    def __init__(self) -> None:
        self._soft_deadline_s = _soft_deadline_s()

    async def solve(self, problem: Problem, services: Services) -> AgentResult:
        challenge = problem.challenge
        start = time.monotonic()
        state = _State()
        best_gate1: str | None = None
        best_gate0: str | None = None

        text, _ = await self._complete(
            services, state, start, REASONER, _draft_messages(problem),
            DRAFT_MAX_TOKENS, DRAFT_TEMPERATURE, reasoning=True,
        )
        candidate = _extract_lean(text, challenge) if text else challenge
        services.checkpoint(candidate, {"stage": "draft"})

        escalations = 0
        while not state.halted:
            ok, errors = _gate0(challenge, candidate)
            if not ok:
                if escalations >= MAX_ESCALATIONS:
                    break
                escalations += 1
                text, _ = await self._complete(
                    services, state, start, REASONER,
                    _escalate_messages(problem, candidate, "; ".join(errors)),
                    DRAFT_MAX_TOKENS, DRAFT_TEMPERATURE, reasoning=True,
                )
                if state.halted:
                    break
                candidate = _extract_lean(text, candidate)
                services.checkpoint(candidate, {"stage": "escalation", "n": escalations})
                continue

            best_gate0 = candidate
            check = await services.lean.check_file(candidate)
            if check.accepted:
                best_gate1 = candidate
                services.checkpoint(candidate, {"stage": "solved"})
                return self._result(candidate, state, escalations, "solved")

            feedback = _format_messages(check.messages) or "The candidate did not compile."
            for attempt in range(1, MAX_QWEN_RETRIES + 1):
                text, _ = await self._complete(
                    services, state, start, DEBUGGER,
                    _debug_messages(problem, candidate, feedback),
                    DEBUG_MAX_TOKENS, DEBUG_TEMPERATURE,
                )
                if state.halted:
                    break
                candidate = _extract_lean(text, candidate)
                services.checkpoint(candidate, {"stage": "debug", "attempt": attempt})

                gate_ok, gate_errors = _gate0(challenge, candidate)
                if not gate_ok:
                    feedback = "Signature/answer policy violation: " + "; ".join(gate_errors)
                    continue
                best_gate0 = candidate
                check = await services.lean.check_file(candidate)
                if check.accepted:
                    best_gate1 = candidate
                    services.checkpoint(candidate, {"stage": "solved"})
                    return self._result(candidate, state, escalations, "solved")
                feedback = _format_messages(check.messages) or "The candidate did not compile."

            if state.halted or escalations >= MAX_ESCALATIONS:
                break
            escalations += 1
            text, _ = await self._complete(
                services, state, start, REASONER,
                _escalate_messages(problem, candidate, feedback),
                DRAFT_MAX_TOKENS, DRAFT_TEMPERATURE, reasoning=True,
            )
            if state.halted:
                break
            candidate = _extract_lean(text, candidate)
            services.checkpoint(candidate, {"stage": "escalation", "n": escalations})

        fallback = best_gate1 or best_gate0 or challenge
        services.checkpoint(fallback, {"stage": "fallback"})
        return self._result(fallback, state, escalations, "fallback")

    async def _complete(
        self,
        services: Services,
        state: _State,
        start: float,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        *,
        reasoning: bool = False,
    ) -> tuple[str, bool]:
        if state.halted:
            return "", True
        if state.spent_usd >= COST_SOFT_CAP_USD:
            state.halted = True
            return "", True
        if self._soft_deadline_s is not None and (time.monotonic() - start) >= self._soft_deadline_s:
            state.halted = True
            return "", True

        kwargs: dict[str, Any] = {}
        if reasoning:
            kwargs["reasoning"] = {"effort": REASONER_EFFORT}
        try:
            response = await services.llm.complete(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )
        except (BudgetExceeded, BudgetAccountingError, LLMCallError):
            state.halted = True
            return "", True

        state.calls += 1
        state.models.add(model)
        state.spent_usd += float(response.usage.get("cost") or 0.0)
        return response.content, False

    @staticmethod
    def _result(solution: str, state: _State, escalations: int, stage: str) -> AgentResult:
        return AgentResult(
            solution,
            {
                "stage": stage,
                "escalations": escalations,
                "llm_calls": state.calls,
                "approx_spent_usd": round(state.spent_usd, 6),
                "models_used": sorted(state.models),
                "halted_early": state.halted,
            },
        )


def create_agent() -> SubmissionAgent:
    return SubmissionAgent()
