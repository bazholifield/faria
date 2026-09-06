"""Token cost estimation for the run ledger.

Prices are Anthropic first-party API rates in USD per million tokens, current
as of 2026-09. They are a local table on purpose — the API doesn't return
prices, and the alternative (guessing from memory at read time) is how ledgers
end up quietly wrong. When a model's price changes or a new model is used,
update PRICES; an unknown model yields cost None rather than a fabricated
number, and the dashboard shows it as unpriced.

Cache multipliers: reads bill at ~0.1x the input rate, writes at ~1.25x.
"""

from __future__ import annotations

from faria.domain.models import TokenUsage

#: USD per 1M tokens: model id -> (input, output).
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

_PER_TOKEN = 1_000_000


def estimate_cost(model: str | None, usage: TokenUsage) -> float | None:
    """USD for one call, or None if the model isn't in PRICES.

    Matches the exact model id first, then the longest known id the returned
    model starts with — the API echoes back a resolved id which may carry a
    suffix the config didn't ask for.
    """
    rates = _rates_for(model)
    if rates is None:
        return None
    input_rate, output_rate = rates
    cost = (
        usage.input_tokens * input_rate
        + usage.cache_read_tokens * input_rate * CACHE_READ_MULTIPLIER
        + usage.cache_write_tokens * input_rate * CACHE_WRITE_MULTIPLIER
        + usage.output_tokens * output_rate
    ) / _PER_TOKEN
    return round(cost, 6)


def _rates_for(model: str | None) -> tuple[float, float] | None:
    if not model:
        return None
    if model in PRICES:
        return PRICES[model]
    candidates = [known for known in PRICES if model.startswith(known)]
    if not candidates:
        return None
    return PRICES[max(candidates, key=len)]
