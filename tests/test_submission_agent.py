"""Offline tests for the submission agent's control flow.

Every model call and Lean check is faked, so these exercise the scaffold's
decisions (portfolio, repair routing, escalation) without spending budget.
"""

from __future__ import annotations

import pytest

from re_harness import LLMCallError, Problem
from submission.agent import Config, SubmissionAgent

CHALLENGE = """import Mathlib

theorem p (n : \u2115) : n + 0 = n := by
  sorry
"""


def lean_file(proof: str) -> str:
    return f"import Mathlib\n\ntheorem p (n : \u2115) : n + 0 = n := by\n  {proof}\n"


def fenced(proof: str) -> str:
    return f"Here you go:\n```lean\n{lean_file(proof).strip()}\n```"


class FakeResponse:
    def __init__(self, content: str):
        self.content = content
        self.usage = {"cost": 0.0001}


class FakeLLM:
    """Answers by model, in order; falls back to a repeated last reply."""

    def __init__(self, replies: dict[str, list[str]]):
        self.replies = {model: list(items) for model, items in replies.items()}
        self.requests: list[dict] = []

    async def complete(self, **kwargs):
        self.requests.append(kwargs)
        queue = self.replies.get(kwargs["model"]) or [""]
        content = queue.pop(0) if len(queue) > 1 else queue[0]
        return FakeResponse(content)

    def models_called(self) -> list[str]:
        return [request["model"] for request in self.requests]


class FakeCheck:
    def __init__(self, accepted: bool, messages=None):
        self.accepted = accepted
        self.messages = messages or []
        self.has_sorry = False
        self.timed_out = False


class FakeLean:
    """Accepts any source containing one of ``good_markers``."""

    def __init__(self, good_markers: tuple[str, ...] = (), errors: int = 1):
        self.good_markers = good_markers
        self.errors = errors
        self.sources: list[str] = []

    async def check_file(self, source: str):
        self.sources.append(source)
        if any(marker in source for marker in self.good_markers):
            return FakeCheck(True)
        return FakeCheck(
            False,
            [{"severity": "error", "pos": {"line": 3}, "data": "unknown identifier"}] * self.errors,
        )


def role_of(system_prompt: str) -> str:
    """Which pipeline stage a request came from, judged by its system prompt."""

    markers = {
        "competition mathematician": "answer",
        "adjudicating": "adjudicate",
        "proof SKELETON": "sketch",
        "closing a single goal": "fill",
        "compiler-error debugger": "debug",
        "clean rewrite": "escalate",
        "expert mathematician and Lean 4": "draft",
    }
    for marker, role in markers.items():
        if marker in system_prompt:
            return role
    return "unknown"


class RoleLLM:
    """Replies by pipeline stage, so tests do not depend on call ordering."""

    def __init__(self, replies: dict[str, str | list[str]]):
        self.replies = replies
        self.requests: list[dict] = []

    async def complete(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies.get(role_of(kwargs["messages"][0]["content"]), "")
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        return FakeResponse(reply)

    def roles_called(self) -> list[str]:
        return [role_of(request["messages"][0]["content"]) for request in self.requests]


class SorryAwareLean:
    """Models the real REPL: sorries are warnings, everything else is an error."""

    def __init__(self, good: tuple[str, ...] = ("simp",), bad: tuple[str, ...] = ("bogus",)):
        self.good = good
        self.bad = bad
        self.sources: list[str] = []

    async def check_file(self, source: str):
        self.sources.append(source)
        if any(marker in source for marker in self.bad):
            return FakeCheck(False, [{"severity": "error", "data": "unknown tactic"}])
        if "sorry" in source:
            return FakeCheck(False, [{"severity": "warning", "data": "declaration uses 'sorry'"}])
        if all(marker in source for marker in self.good):
            return FakeCheck(True)
        return FakeCheck(False, [{"severity": "error", "data": "unsolved goals"}])


class FakeServices:
    def __init__(self, llm: FakeLLM, lean: FakeLean):
        self.llm = llm
        self.lean = lean
        self.checkpoints: list[tuple[str, dict]] = []

    def checkpoint(self, source, metadata=None):
        self.checkpoints.append((source, metadata or {}))


def debugger_models(llm: FakeLLM) -> list[str]:
    """Models used for repair turns, excluding drafts and rewrites."""

    return [
        request["model"]
        for request in llm.requests
        if "compiler-error debugger" in request["messages"][0]["content"]
    ]


def problem() -> Problem:
    return Problem(id="p", description="prove n + 0 = n", challenge=CHALLENGE)


async def run(config: Config, llm: FakeLLM, lean: FakeLean):
    services = FakeServices(llm, lean)
    result = await SubmissionAgent(config).solve(problem(), services)
    return result, services


QWEN = "qwen/qwen3.5-flash-02-23"
OSS = "openai/gpt-oss-120b"


@pytest.mark.asyncio
async def test_portfolio_calls_both_models_in_the_first_wave():
    llm = FakeLLM({OSS: [fenced("ring")], QWEN: [fenced("simp")]})
    result, _ = await run(Config(portfolio_n=4), llm, FakeLean(("simp",)))

    first_wave = llm.models_called()[:4]
    assert set(first_wave) == {OSS, QWEN}
    assert result.metadata["stage"] == "solved"


@pytest.mark.asyncio
async def test_portfolio_recovers_a_problem_only_the_other_model_solves():
    """The reasoner's draft never compiles; Qwen's draft does."""

    llm = FakeLLM({OSS: [fenced("ring")], QWEN: [fenced("simp")]})
    result, services = await run(Config(portfolio_n=2), llm, FakeLean(("simp",)))

    assert "simp" in result.solution
    assert result.metadata["stage"] == "solved"
    assert services.checkpoints[-1][1]["stage"] == "solved"


@pytest.mark.asyncio
async def test_single_draft_configuration_falls_back_to_repair():
    """With portfolio_n=1 only the reasoner drafts, so Qwen must repair it."""

    llm = FakeLLM({OSS: [fenced("ring")], QWEN: [fenced("simp")]})
    result, _ = await run(Config(portfolio_n=1), llm, FakeLean(("simp",)))

    assert llm.models_called()[0] == OSS
    assert llm.models_called()[1] == QWEN
    assert result.metadata["stage"] == "solved"


@pytest.mark.asyncio
async def test_gate0_rejects_an_altered_statement_without_compiling_it():
    """A draft that renames the theorem never reaches the Lean REPL."""

    tampered = "```lean\nimport Mathlib\n\ntheorem other (n : \u2115) : True := by\n  trivial\n```"
    llm = FakeLLM({OSS: [tampered], QWEN: [fenced("simp")]})
    lean = FakeLean(("simp",))
    await run(Config(portfolio_n=2), llm, lean)

    assert all("theorem other" not in source for source in lean.sources)


@pytest.mark.asyncio
async def test_unsolvable_problem_returns_the_best_compiling_shape():
    llm = FakeLLM({OSS: [fenced("ring")], QWEN: [fenced("omega")]})
    result, _ = await run(Config(portfolio_n=2), llm, FakeLean(()))

    assert result.metadata["stage"] == "fallback"
    assert "theorem p" in result.solution
    assert "sorry" not in result.solution


@pytest.mark.asyncio
async def test_smart_repair_sends_a_qwen_draft_to_the_reasoner():
    """Cross-model routing: the critic is never the author."""

    llm = FakeLLM({OSS: [fenced("ring")], QWEN: [fenced("omega")]})
    await run(Config(portfolio_n=2, smart_repair=True), llm, FakeLean(()))

    # The reasoner's draft is repaired by Qwen and Qwen's draft by the reasoner,
    # so both models appear as debuggers across the two repair seeds.
    assert set(debugger_models(llm)) == {OSS, QWEN}


@pytest.mark.asyncio
async def test_plain_repair_always_routes_to_qwen():
    llm = FakeLLM({OSS: [fenced("ring")], QWEN: [fenced("omega")]})
    await run(Config(portfolio_n=2, smart_repair=False), llm, FakeLean(()))

    assert set(debugger_models(llm)) == {QWEN}


@pytest.mark.asyncio
async def test_repeated_error_signature_stops_the_repair_loop_early():
    """A repair that reproduces the same errors ends the loop before its cap."""

    llm = FakeLLM({OSS: [fenced("ring")], QWEN: [fenced("omega")]})
    stalling = FakeLean(())

    result, _ = await run(Config(portfolio_n=1, smart_repair=True), llm, stalling)
    stalled = [entry for entry in result.metadata["trace"] if entry["stage"] == "stalled"]
    repairs = [entry for entry in result.metadata["trace"] if entry["stage"] == "repair"]

    assert stalled, "expected the no-progress guard to fire"
    # Five repairs per escalation round would be the cap; stalling cuts it to one.
    assert len(repairs) < 5 * (1 + 2)


NUMERIC_CHALLENGE = """import Mathlib

abbrev q_answer : \u2115 := sorry

theorem q : 7 ^ 2 % 100 = q_answer := by
  sorry
"""


def numeric_solution(value: str) -> str:
    return (
        "```lean\nimport Mathlib\n\n"
        f"abbrev q_answer : \u2115 := {value}\n\n"
        "theorem q : 7 ^ 2 % 100 = q_answer := by\n  decide\n```"
    )


def numeric_problem() -> Problem:
    return Problem(id="q", description="last two digits", challenge=NUMERIC_CHALLENGE)


async def run_numeric(config: Config, llm: FakeLLM, lean: FakeLean):
    services = FakeServices(llm, lean)
    result = await SubmissionAgent(config).solve(numeric_problem(), services)
    return result, services


@pytest.mark.asyncio
async def test_agreed_answer_is_pinned_into_the_drafting_prompt():
    llm = FakeLLM({
        OSS: ["reasoning...\nANSWER q_answer = 49", numeric_solution("49")],
        QWEN: ["reasoning...\nANSWER q_answer = 49", numeric_solution("49")],
    })
    result, _ = await run_numeric(Config(portfolio_n=2, answer_first=True), llm, FakeLean(("decide",)))

    assert result.metadata["answer_agreement"] == "agreed"
    assert result.metadata["answers"] == {"q_answer": "49"}
    draft_prompt = llm.requests[2]["messages"][1]["content"]
    assert "q_answer = 49" in draft_prompt


@pytest.mark.asyncio
async def test_disagreement_triggers_one_adjudication_round():
    llm = FakeLLM({
        OSS: ["ANSWER q_answer = 49", "on reflection\nANSWER q_answer = 49", numeric_solution("49")],
        QWEN: ["ANSWER q_answer = 51", "I concede\nANSWER q_answer = 49", numeric_solution("49")],
    })
    result, _ = await run_numeric(Config(portfolio_n=2, answer_first=True), llm, FakeLean(("decide",)))

    assert result.metadata["answer_agreement"] == "adjudicated"
    assert result.metadata["answers"] == {"q_answer": "49"}
    assert any(
        "adjudicating" in request["messages"][0]["content"].lower() for request in llm.requests
    )


@pytest.mark.asyncio
async def test_answer_first_is_skipped_for_pure_proof_problems():
    llm = FakeLLM({OSS: [fenced("ring")], QWEN: [fenced("simp")]})
    result, _ = await run(Config(portfolio_n=2, answer_first=True), llm, FakeLean(("simp",)))

    assert result.metadata["answer_agreement"] == "not_applicable"
    assert all(
        "competition mathematician" not in request["messages"][0]["content"]
        for request in llm.requests
    )


@pytest.mark.asyncio
async def test_answer_first_can_be_disabled():
    llm = FakeLLM({OSS: [numeric_solution("49")], QWEN: [numeric_solution("49")]})
    result, _ = await run_numeric(
        Config(portfolio_n=2, answer_first=False), llm, FakeLean(("decide",))
    )

    assert result.metadata["answer_agreement"] == "not_applicable"
    assert llm.requests[0]["messages"][0]["content"].startswith("You are an expert mathematician")


SKELETON = """```lean
import Mathlib

theorem p (n : \u2115) : n + 0 = n := by
  have step1 : n + 0 = n := by sorry
  exact step1
```"""


def sketch_config(**overrides) -> Config:
    base = dict(portfolio_n=1, smart_repair=False, answer_first=False, sketch_fill=True)
    base.update(overrides)
    return Config(**base)


@pytest.mark.asyncio
async def test_sketch_then_fill_solves_what_whole_file_drafting_cannot():
    llm = RoleLLM({
        "draft": fenced("ring"),
        "debug": fenced("ring"),
        "escalate": fenced("ring"),
        "sketch": SKELETON,
        "fill": "```lean\nsimp\n```",
    })
    services = FakeServices(llm, SorryAwareLean())
    result = await SubmissionAgent(sketch_config()).solve(problem(), services)

    assert result.metadata["stage"] == "solved"
    assert "simp" in result.solution
    assert "sorry" not in result.solution


@pytest.mark.asyncio
async def test_a_gap_fill_that_breaks_the_file_is_reverted_and_retried():
    """Qwen's suggestion introduces an error, so the reasoner gets the gap."""

    llm = RoleLLM({
        "draft": fenced("ring"),
        "debug": fenced("ring"),
        "escalate": fenced("ring"),
        "sketch": SKELETON,
        "fill": ["```lean\nbogus_tactic\n```", "```lean\nsimp\n```"],
    })
    services = FakeServices(llm, SorryAwareLean())
    result = await SubmissionAgent(sketch_config()).solve(problem(), services)

    fills = [entry for entry in result.metadata["trace"] if entry["stage"] == "fill"]
    assert [entry["outcome"] for entry in fills] == ["rejected", "solved"]
    assert "bogus_tactic" not in result.solution


@pytest.mark.asyncio
async def test_a_skeleton_that_does_not_elaborate_is_not_filled():
    llm = RoleLLM({
        "draft": fenced("ring"),
        "debug": fenced("ring"),
        "escalate": fenced("ring"),
        "sketch": "```lean\nimport Mathlib\n\ntheorem p (n : \u2115) : n + 0 = n := by\n"
                  "  have step1 : n + 0 = n := by bogus\n  sorry\n```",
        "fill": "```lean\nsimp\n```",
    })
    services = FakeServices(llm, SorryAwareLean())
    result = await SubmissionAgent(sketch_config()).solve(problem(), services)

    assert result.metadata["stage"] == "fallback"
    assert "fill" not in llm.roles_called()


@pytest.mark.asyncio
async def test_sketch_stage_can_be_disabled():
    llm = RoleLLM({
        "draft": fenced("ring"),
        "debug": fenced("ring"),
        "escalate": fenced("ring"),
        "sketch": SKELETON,
        "fill": "```lean\nsimp\n```",
    })
    services = FakeServices(llm, SorryAwareLean())
    await SubmissionAgent(sketch_config(sketch_fill=False)).solve(problem(), services)

    assert "sketch" not in llm.roles_called()


@pytest.mark.asyncio
async def test_second_portfolio_candidate_is_pursued_when_the_first_fails():
    llm = FakeLLM({OSS: [fenced("ring")], QWEN: [fenced("omega")]})
    lean = FakeLean(())
    result, _ = await run(Config(portfolio_n=2, smart_repair=True, max_rounds=1), llm, lean)

    escalations = [entry for entry in result.metadata["trace"] if entry["stage"] == "escalation"]
    # Two seeds, each escalating twice after its repair budget is spent.
    assert len(escalations) == 4


@pytest.mark.asyncio
async def test_a_failed_round_is_retried_with_fresh_samples():
    llm = FakeLLM({OSS: [fenced("ring")], QWEN: [fenced("omega")]})
    result, _ = await run(
        Config(portfolio_n=2, smart_repair=False, sketch_fill=False, max_rounds=2),
        llm,
        FakeLean(()),
    )

    assert result.metadata["rounds"] == 2
    portfolios = [entry for entry in result.metadata["trace"] if entry["stage"] == "portfolio"]
    assert [entry["round"] for entry in portfolios] == [0, 1]
    # The second round must not repeat the first round's sampling.
    draft_seeds = [
        request.get("seed")
        for request in llm.requests
        if role_of(request["messages"][0]["content"]) == "draft"
    ]
    assert len(draft_seeds) == 4
    assert len(set(draft_seeds[2:]) & set(draft_seeds[:2])) == 0


@pytest.mark.asyncio
async def test_an_unfinished_file_is_never_returned_as_the_fallback():
    """A draft that leaves `sorry` in place has no errors but scores zero."""

    llm = FakeLLM({
        OSS: ["```lean\nimport Mathlib\n\ntheorem p (n : \u2115) : n + 0 = n := by\n  sorry\n```"],
        QWEN: [fenced("omega")],
    })
    result, _ = await run(
        Config(portfolio_n=2, sketch_fill=False, max_rounds=1), llm, SorryAwareLean()
    )

    assert result.metadata["stage"] == "fallback"
    assert "omega" in result.solution


class FlakyLLM(RoleLLM):
    """Refuses the first `failures` calls the way a rate limiter would."""

    def __init__(self, replies, failures: int, message: str):
        super().__init__(replies)
        self.remaining = failures
        self.message = message

    async def complete(self, **kwargs):
        if self.remaining:
            self.remaining -= 1
            raise LLMCallError(self.message)
        return await super().complete(**kwargs)


@pytest.mark.asyncio
async def test_a_rate_limited_call_is_retried(monkeypatch):
    monkeypatch.setattr("submission.agent.RATE_LIMIT_BACKOFF_S", 0.0)
    llm = FlakyLLM(
        {"draft": fenced("simp")},
        failures=2,
        message="OpenRouter returned HTTP 429; the request was refused and reported no cost: {}",
    )
    services = FakeServices(llm, FakeLean(("simp",)))
    result = await SubmissionAgent(sketch_config(answer_first=False)).solve(problem(), services)

    assert result.metadata["stage"] == "solved"
    retries = [entry for entry in result.metadata["trace"] if entry["stage"] == "rate_limited"]
    assert len(retries) == 2


@pytest.mark.asyncio
async def test_a_failure_with_uncertain_spend_stops_immediately(monkeypatch):
    monkeypatch.setattr("submission.agent.RATE_LIMIT_BACKOFF_S", 0.0)
    llm = FlakyLLM(
        {"draft": fenced("simp")},
        failures=1,
        message="OpenRouter request failed; spend is uncertain: boom",
    )
    services = FakeServices(llm, FakeLean(("simp",)))
    result = await SubmissionAgent(sketch_config(answer_first=False)).solve(problem(), services)

    assert result.metadata["halted_early"] is True
    assert result.metadata["llm_calls"] == 0


def test_phase_ladder_environment_variables_select_the_documented_configs(monkeypatch):
    for name in (
        "AGENT_PORTFOLIO_N",
        "AGENT_SMART_REPAIR",
        "AGENT_ANSWER_FIRST",
        "AGENT_SKETCH_FILL",
        "AGENT_MAX_ROUNDS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert Config.from_env() == Config(
        portfolio_n=4, smart_repair=True, answer_first=True, sketch_fill=True, max_rounds=2
    )

    monkeypatch.setenv("AGENT_PORTFOLIO_N", "1")
    monkeypatch.setenv("AGENT_SMART_REPAIR", "0")
    monkeypatch.setenv("AGENT_ANSWER_FIRST", "0")
    monkeypatch.setenv("AGENT_SKETCH_FILL", "0")
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "1")
    assert Config.from_env() == Config(
        portfolio_n=1, smart_repair=False, answer_first=False, sketch_fill=False, max_rounds=1
    )


@pytest.mark.asyncio
async def test_a_solved_problem_does_not_start_another_round():
    llm = FakeLLM({OSS: [fenced("ring")], QWEN: [fenced("simp")]})
    result, _ = await run(Config(portfolio_n=2, max_rounds=3), llm, FakeLean(("simp",)))

    assert result.metadata["rounds"] == 1
    assert result.metadata["stage"] == "solved"
