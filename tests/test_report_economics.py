"""Unit tests for agenteval.report.economics (pure functions)."""

from __future__ import annotations

import math

import pytest
from inspect_ai.event import ModelEvent
from inspect_ai.log import EvalSample
from inspect_ai.model import (
    ChatCompletionChoice,
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageUser,
    GenerateConfig,
    ModelOutput,
    ModelUsage,
)
from inspect_ai.tool import ToolCall

from agenteval.config import Pricing
from agenteval.report.economics import (
    SampleEconomics,
    TokenUsage,
    count_model_calls,
    count_tool_calls,
    per_resolved,
    pricing_cost,
    rate,
    resolve_cost,
    sample_economics,
    split_usage,
    sum_usage,
    summarize,
    usage_economics,
)

PRICING = {"input": 0.24, "output": 2.20, "input_cache_read": 0.06, "input_cache_write": 0.30}


def _calls(n: int) -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", function="bash", arguments={"cmd": "ls"}) for i in range(n)]


def model_event(tool_calls: int = 0, role: str | None = None, choices: bool = True) -> ModelEvent:
    message = ChatMessageAssistant(content="ok", tool_calls=_calls(tool_calls) or None)
    output = ModelOutput(
        model="m",
        choices=[ChatCompletionChoice(message=message, stop_reason="stop")] if choices else [],
    )
    return ModelEvent(
        model="m",
        role=role,
        input=[],
        tools=[],
        tool_choice="auto",
        config=GenerateConfig(),
        output=output,
    )


def test_from_model_usage_flattens_optional_fields() -> None:
    usage = TokenUsage.from_model_usage(
        ModelUsage(
            input_tokens=100,
            output_tokens=50,
            total_tokens=0,
            input_tokens_cache_read=30,
            input_tokens_cache_write=None,
            reasoning_tokens=7,
            total_cost=float("nan"),
        )
    )
    assert usage.input == 100
    assert usage.cache_read == 30
    assert usage.cache_write == 0
    assert usage.reasoning == 7
    assert usage.total == 180  # computed when the provider reports 0
    assert usage.cost is None  # non-finite costs are dropped


def test_add_and_minus() -> None:
    a = TokenUsage(input=10, output=5, total=15, cost=1.0)
    b = TokenUsage(input=4, output=1, total=5, cost=None)
    assert (a + b).input == 14
    # tokens without a cost make the sum unknown rather than understated
    assert (a + b).cost is None
    assert (b + a).cost is None
    assert (b + b).cost is None
    assert (a + TokenUsage()).cost == 1.0  # no tokens, no cost: neutral
    assert (TokenUsage() + TokenUsage()).cost is None
    diff = a.minus(TokenUsage(input=20, output=1, total=21, cost=0.25))
    assert diff.input == 0  # clamped
    assert diff.output == 4
    assert diff.cost == pytest.approx(0.75)
    assert a.minus(b).cost is None  # unknown share of the cost to subtract
    assert a.minus(TokenUsage()).cost == 1.0
    assert sum_usage([a, a, a]).total == 45
    assert sum_usage([a, a, a]).cost == pytest.approx(3.0)


def test_split_usage_excludes_grader_role() -> None:
    model_usage = {
        "openrouter/qwen": ModelUsage(input_tokens=1000, output_tokens=200, total_tokens=1200)
    }
    role_usage = {"grader": ModelUsage(input_tokens=300, output_tokens=20, total_tokens=320)}
    agent, scoring = split_usage(model_usage, role_usage)
    assert (agent.input, agent.output, agent.total) == (700, 180, 880)
    assert scoring.total == 320


def test_split_usage_unpriced_model_is_not_free() -> None:
    # unpriced agent model next to a priced grader model: the agent's cost is
    # unknown (falls back to profile pricing), not total - grader = $0
    model_usage = {
        "openrouter/agent": ModelUsage(input_tokens=1000, total_tokens=1000),
        "openai/grader": ModelUsage(input_tokens=100, total_tokens=100, total_cost=0.01),
    }
    role_usage = {"grader": model_usage["openai/grader"]}
    agent, scoring = split_usage(model_usage, role_usage)
    assert agent.total == 1000
    assert agent.cost is None
    assert scoring.cost == pytest.approx(0.01)
    assert resolve_cost(agent, None) == (None, "unknown")
    cost, source = resolve_cost(agent, {"input": 1.0, "output": 1.0})
    assert source == "pricing"
    assert cost == pytest.approx(0.001)

    econ = usage_economics(model_usage, role_usage, None)
    assert econ.cost_usd is None and econ.cost_source == "unknown"
    assert econ.scoring_usage.total == 100


def test_split_usage_keeps_non_scorer_roles() -> None:
    model_usage = {"m": ModelUsage(input_tokens=10, output_tokens=10, total_tokens=20)}
    agent, scoring = split_usage(model_usage, {"agent": model_usage["m"]})
    assert agent.total == 20
    assert scoring.total == 0


def test_pricing_cost() -> None:
    usage = TokenUsage(input=1_000_000, output=500_000, cache_read=2_000_000, cache_write=100_000)
    expected = 0.24 + 0.5 * 2.20 + 2 * 0.06 + 0.1 * 0.30
    assert pricing_cost(usage, PRICING) == pytest.approx(expected)
    # no cache rates: cache tokens are charged at the input rate, not for free
    assert pricing_cost(usage, {"input": 1.0, "output": None}) == pytest.approx(1.0 + 2.0 + 0.1)
    explicit = {"input": 1.0, "output": None, "input_cache_read": 0, "input_cache_write": None}
    assert pricing_cost(usage, explicit) == pytest.approx(1.0 + 0.1)
    assert pricing_cost(usage, None) is None
    assert pricing_cost(usage, {}) is None


def test_pricing_cost_cache_rates_default_to_input() -> None:
    # config.Pricing dumped into task metadata carries None for unset cache rates
    pricing = Pricing(input=0.24, output=2.20).model_dump()
    assert pricing["input_cache_read"] is None
    usage = TokenUsage(input=2000, cache_read=8000, output=100, total=10_100)
    assert pricing_cost(usage, pricing) == pytest.approx(0.00262)


def test_resolve_cost_precedence() -> None:
    assert resolve_cost(TokenUsage(input=10, total=10, cost=0.5), PRICING) == (0.5, "log")
    cost, source = resolve_cost(TokenUsage(input=1_000_000, total=1_000_000), PRICING)
    assert source == "pricing"
    assert cost == pytest.approx(0.24)
    assert resolve_cost(TokenUsage(), None) == (0.0, "no-usage")
    assert resolve_cost(TokenUsage(input=5, total=5), None) == (None, "unknown")


def test_count_model_and_tool_calls_from_events() -> None:
    events = [model_event(2), model_event(0), model_event(1), model_event(3, role="grader")]
    assert count_model_calls(events) == 3
    assert count_tool_calls(events, []) == 3
    # outputs without choices (failed calls) count as calls but have no tool calls
    assert count_tool_calls([model_event(choices=False)], []) == 0
    assert count_model_calls([model_event(choices=False)]) == 1


def test_count_tool_calls_falls_back_to_messages() -> None:
    messages: list[ChatMessage] = [
        ChatMessageUser(content="fix it"),
        ChatMessageAssistant(content="", tool_calls=_calls(2)),
        ChatMessageAssistant(content="", tool_calls=_calls(1)),
    ]
    assert count_tool_calls([], messages) == 3


def test_sample_economics() -> None:
    sample = EvalSample(
        id=1,
        epoch=1,
        input="x",
        target="",
        model_usage={
            "m": ModelUsage(input_tokens=100_000, output_tokens=20_000, total_tokens=120_000)
        },
        role_usage={},
        events=[model_event(2), model_event(1)],
        total_time=12.5,
        working_time=10.0,
    )
    econ = sample_economics(sample, PRICING)
    assert econ.usage.total == 120_000
    assert econ.cost_source == "pricing"
    assert econ.cost_usd == pytest.approx(0.024 + 0.044)
    assert econ.model_calls == 2
    assert econ.tool_calls == 3
    assert econ.total_time == 12.5


def test_summarize_and_per_resolved() -> None:
    samples = [
        SampleEconomics(
            usage=TokenUsage(input=100, output=50, total=150),
            cost_usd=0.10,
            cost_source="log",
            model_calls=3,
            tool_calls=5,
            total_time=10.0,
            working_time=8.0,
        ),
        SampleEconomics(
            usage=TokenUsage(input=200, output=50, total=250),
            cost_usd=0.30,
            cost_source="pricing",
            model_calls=1,
            tool_calls=0,
            total_time=30.0,
            working_time=None,
        ),
        SampleEconomics(usage=TokenUsage(input=1, total=1), cost_usd=None, cost_source="unknown"),
    ]
    summary = summarize(samples, resolved=2, errors=1)
    assert summary.samples == 3
    assert summary.usage.total == 401
    assert summary.cost_usd == pytest.approx(0.40)
    assert summary.cost_complete is False
    assert summary.model_calls == 4
    assert summary.tool_calls == 5
    assert summary.wall_time == pytest.approx(40.0)
    assert summary.working_time == pytest.approx(8.0)
    assert summary.mean_wall_time == pytest.approx(20.0)
    assert summary.cost_per_resolved is None  # cost incomplete
    assert summary.tokens_per_resolved == pytest.approx(200.5)

    complete = summarize(samples[:2], resolved=2)
    assert complete.cost_complete is True
    assert complete.cost_per_resolved == pytest.approx(0.20)

    empty = summarize([])
    assert empty.cost_usd is None
    assert empty.cost_complete is True
    assert empty.tokens_per_resolved is None
    assert empty.mean_wall_time is None


def test_summarize_unpriced_group_has_no_cost() -> None:
    unknown = SampleEconomics(usage=TokenUsage(input=1000, total=1000), cost_source="unknown")
    no_usage = SampleEconomics(cost_usd=0.0, cost_source="no-usage")
    summary = summarize([unknown, no_usage], resolved=1)
    assert summary.cost_usd is None  # not $0.00
    assert summary.cost_complete is False
    assert summary.cost_per_resolved is None

    only_no_usage = summarize([no_usage, no_usage])
    assert only_no_usage.cost_usd == 0.0
    assert only_no_usage.cost_complete is True


def test_rate_helpers() -> None:
    assert per_resolved(1.0, 0) is None
    assert per_resolved(None, 3) is None
    assert per_resolved(3.0, 3) == 1.0
    assert rate(1, 4) == 0.25
    assert rate(0, 0) is None
    assert not math.isnan(rate(0, 1) or 0.0)
