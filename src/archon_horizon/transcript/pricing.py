"""Optional local token-cost estimation for transcript usage events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .model import TranscriptEvent, TranscriptKind, TranscriptUsage
from .parsers import TranscriptParser


@dataclass(frozen=True, slots=True)
class TokenPricing:
    """USD prices per one million tokens.

    Provider CLIs may emit token counts without cost. This model lets a
    workspace opt into approximate cost telemetry without baking provider
    pricing into Archon Horizon.
    """

    input_per_million_usd: float
    output_per_million_usd: float
    cached_input_per_million_usd: float | None = None


def pricing_from_mapping(data: object) -> TokenPricing | None:
    if not isinstance(data, Mapping):
        return None
    if "input_per_million_usd" not in data or "output_per_million_usd" not in data:
        return None
    cached = data.get("cached_input_per_million_usd")
    return TokenPricing(
        input_per_million_usd=float(data["input_per_million_usd"]),
        output_per_million_usd=float(data["output_per_million_usd"]),
        cached_input_per_million_usd=float(cached) if cached is not None else None,
    )


def estimate_cost_usd(usage: TranscriptUsage, pricing: TokenPricing) -> float:
    cached_input = max(usage.cached_tokens_in, 0)
    if pricing.cached_input_per_million_usd is None:
        billable_input = max(usage.tokens_in, 0)
        cached_cost = 0.0
    else:
        billable_input = max(usage.tokens_in - cached_input, 0)
        cached_cost = cached_input * pricing.cached_input_per_million_usd / 1_000_000
    input_cost = billable_input * pricing.input_per_million_usd / 1_000_000
    output_cost = max(usage.tokens_out, 0) * pricing.output_per_million_usd / 1_000_000
    return input_cost + cached_cost + output_cost


def price_event(event: TranscriptEvent, pricing: TokenPricing) -> TranscriptEvent:
    usage = event.usage
    if usage is None or usage.cost_usd is not None:
        return event
    priced_usage = replace(usage, cost_usd=estimate_cost_usd(usage, pricing))
    if event.kind is not TranscriptKind.USAGE:
        return replace(event, usage=priced_usage)
    data: dict[str, Any] = {
        **event.data,
        "cost_usd": priced_usage.cost_usd,
        "cost_estimated": True,
    }
    return replace(event, usage=priced_usage, data=data)


def with_usage_pricing(parser: TranscriptParser, pricing: TokenPricing | None) -> TranscriptParser:
    if pricing is None:
        return parser

    def parse(line: str) -> list[TranscriptEvent]:
        return [price_event(event, pricing) for event in parser(line)]

    return parse
