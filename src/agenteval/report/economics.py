"""Token, cost, call-count and timing arithmetic over Inspect samples.

Pure functions only (no I/O) so the numbers in the report are unit-testable.

Usage of LLM-graded scorers (model role ``grader``, e.g. CodeIPI's detection
check) is split out: it is recorded in a sample's ``model_usage`` but is not
part of what the agent setup under test spent.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from inspect_ai.event import Event, ModelEvent
from inspect_ai.log import EvalSample
from inspect_ai.model import ChatMessage, ChatMessageAssistant, ModelUsage
from pydantic import BaseModel, ConfigDict

SCORER_ROLES: frozenset[str] = frozenset({"grader", "judge", "scorer"})
"""Model roles whose usage belongs to scoring, not to the agent."""

CostSource = Literal["log", "pricing", "no-usage", "unknown"]
"""Where a cost figure came from: Inspect's own ``total_cost`` (prices registered
via ``set_model_info``), the model profile's pricing, no tokens used at all, or
not computable (tokens used but no price known)."""

PricingLike = Mapping[str, float | int | None]
"""``config.Pricing`` as dumped into task metadata: USD per 1M tokens."""


class TokenUsage(BaseModel):
    """Flattened ``ModelUsage`` that can be added and subtracted."""

    model_config = ConfigDict(frozen=True)

    input: int = 0
    """Input tokens charged at full rate (excludes cache reads/writes)."""
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    """Already included in ``output`` by most providers; reported separately."""
    total: int = 0
    cost: float | None = None
    """USD as recorded by Inspect; ``None`` when no price was registered."""

    @classmethod
    def from_model_usage(cls, usage: ModelUsage) -> TokenUsage:
        cache_read = usage.input_tokens_cache_read or 0
        cache_write = usage.input_tokens_cache_write or 0
        total = usage.total_tokens or (
            usage.input_tokens + usage.output_tokens + cache_read + cache_write
        )
        return cls(
            input=usage.input_tokens,
            output=usage.output_tokens,
            cache_read=cache_read,
            cache_write=cache_write,
            reasoning=usage.reasoning_tokens or 0,
            total=total,
            cost=_finite(usage.total_cost),
        )

    @property
    def cost_unknown(self) -> bool:
        """Tokens were used but no cost was recorded for them."""
        return self.cost is None and self.total > 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        # an unpriced part with tokens makes the sum unknown, not cheaper
        cost: float | None = None
        if not (self.cost_unknown or other.cost_unknown):
            cost = _optional_sum(self.cost, other.cost)
        return TokenUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            reasoning=self.reasoning + other.reasoning,
            total=self.total + other.total,
            cost=cost,
        )

    def minus(self, other: TokenUsage) -> TokenUsage:
        """Subtract ``other`` (a subset of this usage), clamping at zero."""
        cost: float | None = None
        if self.cost is not None and not other.cost_unknown:
            cost = max(0.0, self.cost - (other.cost or 0.0))
        return TokenUsage(
            input=max(0, self.input - other.input),
            output=max(0, self.output - other.output),
            cache_read=max(0, self.cache_read - other.cache_read),
            cache_write=max(0, self.cache_write - other.cache_write),
            reasoning=max(0, self.reasoning - other.reasoning),
            total=max(0, self.total - other.total),
            cost=cost,
        )


def sum_usage(usages: Iterable[TokenUsage]) -> TokenUsage:
    total = TokenUsage()
    for usage in usages:
        total = total + usage
    return total


def split_usage(
    model_usage: Mapping[str, ModelUsage],
    role_usage: Mapping[str, ModelUsage],
    scorer_roles: frozenset[str] = SCORER_ROLES,
) -> tuple[TokenUsage, TokenUsage]:
    """Return ``(agent, scoring)`` usage for one sample.

    ``model_usage`` holds every call made in the sample; ``role_usage`` holds the
    calls made through a model role, so scorer roles are subtracted from the total.
    """
    total = sum_usage(TokenUsage.from_model_usage(u) for u in model_usage.values())
    scoring = sum_usage(
        TokenUsage.from_model_usage(u) for role, u in role_usage.items() if role in scorer_roles
    )
    return total.minus(scoring), scoring


def pricing_cost(usage: TokenUsage, pricing: PricingLike | None) -> float | None:
    """Cost in USD of ``usage`` at ``pricing`` (USD per 1M tokens), or ``None``.

    A missing or ``None`` cache rate means no cache discount: those tokens are
    charged at the ``input`` rate (as ``config.Pricing`` does). An explicit rate,
    including 0, applies as given.
    """
    if not pricing:
        return None

    def rate(key: str, fallback: float = 0.0) -> float:
        value = pricing.get(key)
        return float(value) if value is not None else fallback

    input_rate = rate("input")
    cost = (
        usage.input * input_rate
        + usage.output * rate("output")
        + usage.cache_read * rate("input_cache_read", input_rate)
        + usage.cache_write * rate("input_cache_write", input_rate)
    ) / 1_000_000
    return _finite(cost)


def resolve_cost(usage: TokenUsage, pricing: PricingLike | None) -> tuple[float | None, CostSource]:
    """Prefer Inspect's recorded cost, then the profile pricing."""
    if usage.cost is not None:
        return usage.cost, "log"
    computed = pricing_cost(usage, pricing)
    if computed is not None:
        return computed, "pricing"
    if usage.total == 0:
        return 0.0, "no-usage"
    return None, "unknown"


def count_model_calls(events: Sequence[Event], scorer_roles: frozenset[str] = SCORER_ROLES) -> int:
    """Model calls made on behalf of the agent (scorer roles excluded)."""
    return sum(1 for e in _agent_model_events(events, scorer_roles))


def count_tool_calls(
    events: Sequence[Event],
    messages: Sequence[ChatMessage],
    scorer_roles: frozenset[str] = SCORER_ROLES,
) -> int:
    """Tool calls the model requested.

    Counted from model outputs, which also covers bridged CLI agents whose tools
    run outside Inspect (no ToolEvents). Falls back to the final message list
    when the sample has no model events (e.g. events were not logged).
    """
    model_events = list(_agent_model_events(events, scorer_roles))
    if model_events:
        return sum(
            len(e.output.choices[0].message.tool_calls or [])
            for e in model_events
            if e.output.choices
        )
    return sum(len(m.tool_calls or []) for m in messages if isinstance(m, ChatMessageAssistant))


def usage_by_model(
    events: Sequence[Event], scorer_roles: frozenset[str] = SCORER_ROLES
) -> dict[str, TokenUsage]:
    """Agent usage per model (e.g. orchestrator vs subagent model), from model events."""
    by_model: dict[str, TokenUsage] = {}
    for event in _agent_model_events(events, scorer_roles):
        if event.output.usage is not None:
            usage = TokenUsage.from_model_usage(event.output.usage)
            by_model[event.model] = by_model.get(event.model, TokenUsage()) + usage
    return by_model


def merge_by_model(groups: Iterable[Mapping[str, TokenUsage]]) -> dict[str, TokenUsage]:
    merged: dict[str, TokenUsage] = {}
    for group in groups:
        for model, usage in group.items():
            merged[model] = merged.get(model, TokenUsage()) + usage
    return merged


def _agent_model_events(
    events: Sequence[Event], scorer_roles: frozenset[str]
) -> Iterable[ModelEvent]:
    for event in events:
        if isinstance(event, ModelEvent) and not event.pending and event.role not in scorer_roles:
            yield event


class SampleEconomics(BaseModel):
    """Economics of one sample (one epoch of one dataset item)."""

    usage: TokenUsage = TokenUsage()
    """Agent usage (scoring excluded)."""
    scoring_usage: TokenUsage = TokenUsage()
    cost_usd: float | None = None
    cost_source: CostSource = "no-usage"
    model_calls: int = 0
    tool_calls: int = 0
    total_time: float | None = None
    """Wall-clock seconds for the sample (setup, agent, scoring)."""
    working_time: float | None = None
    """Seconds spent working (excludes waiting on rate limits / shared resources)."""
    by_model: dict[str, TokenUsage] = {}
    """Agent usage per model, from model events (empty when events were not logged)."""


def sample_economics(sample: EvalSample, pricing: PricingLike | None) -> SampleEconomics:
    econ = usage_economics(sample.model_usage, sample.role_usage, pricing)
    econ.model_calls = count_model_calls(sample.events)
    econ.tool_calls = count_tool_calls(sample.events, sample.messages)
    econ.by_model = usage_by_model(sample.events)
    econ.total_time = _finite(sample.total_time)
    econ.working_time = _finite(sample.working_time)
    return econ


def usage_economics(
    model_usage: Mapping[str, ModelUsage],
    role_usage: Mapping[str, ModelUsage],
    pricing: PricingLike | None,
) -> SampleEconomics:
    """Tokens and cost only (no call counts or timing), e.g. from a sample summary."""
    usage, scoring = split_usage(model_usage, role_usage)
    # the profile pricing belongs to the task's model; it cannot price a sample whose
    # calls also went to other models (e.g. subagents on their own model)
    cost, source = resolve_cost(usage, pricing if len(model_usage) <= 1 else None)
    return SampleEconomics(usage=usage, scoring_usage=scoring, cost_usd=cost, cost_source=source)


class EconomicsSummary(BaseModel):
    """Economics aggregated over a group of samples (e.g. one config of one suite)."""

    samples: int = 0
    resolved: int = 0
    errors: int = 0
    usage: TokenUsage = TokenUsage()
    scoring_usage: TokenUsage = TokenUsage()
    scoring_cost_usd: float | None = None
    cost_usd: float | None = None
    """Sum over samples with a known cost; see ``cost_complete``. ``None`` when no
    sample that used tokens has a known cost."""
    cost_complete: bool = True
    """False when some samples used tokens but have no known price."""
    model_calls: int = 0
    tool_calls: int = 0
    wall_time: float = 0.0
    """Sum of per-sample wall-clock time (seconds); samples may have run in parallel."""
    working_time: float = 0.0
    mean_wall_time: float | None = None
    cost_per_resolved: float | None = None
    """``None`` unless the cost is complete."""
    tokens_per_resolved: float | None = None
    by_model: dict[str, TokenUsage] = {}
    """Agent usage per model; more than one entry when subagents run on their own model."""


def summarize(
    samples: Sequence[SampleEconomics], *, resolved: int = 0, errors: int = 0
) -> EconomicsSummary:
    usage = sum_usage(s.usage for s in samples)
    scoring = sum_usage(s.scoring_usage for s in samples)
    known_costs = [s.cost_usd for s in samples if s.cost_usd is not None]
    cost_complete = len(known_costs) == len(samples)
    priced = any(s.cost_source in ("log", "pricing") for s in samples)
    # a group where nothing that used tokens has a price has no cost, not $0
    cost = math.fsum(known_costs) if known_costs and (cost_complete or priced) else None
    wall = [s.total_time for s in samples if s.total_time is not None]
    working = [s.working_time for s in samples if s.working_time is not None]
    scoring_cost, _ = resolve_cost(scoring, None)
    return EconomicsSummary(
        samples=len(samples),
        resolved=resolved,
        errors=errors,
        usage=usage,
        scoring_usage=scoring,
        scoring_cost_usd=scoring_cost if scoring.total else None,
        cost_usd=cost,
        cost_complete=cost_complete,
        model_calls=sum(s.model_calls for s in samples),
        tool_calls=sum(s.tool_calls for s in samples),
        wall_time=math.fsum(wall),
        working_time=math.fsum(working),
        mean_wall_time=math.fsum(wall) / len(wall) if wall else None,
        cost_per_resolved=per_resolved(cost, resolved) if cost_complete else None,
        tokens_per_resolved=per_resolved(float(usage.total), resolved) if samples else None,
        by_model=merge_by_model(s.by_model for s in samples),
    )


def per_resolved(value: float | None, resolved: int) -> float | None:
    if value is None or resolved <= 0:
        return None
    return value / resolved


def rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _optional_sum(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def _finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return float(value)
