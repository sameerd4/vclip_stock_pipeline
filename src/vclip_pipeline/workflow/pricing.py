"""Centralized, versioned provider token pricing for visual enrichment.

Keep rates here — never hard-code them inside provider adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Bump when rates or the pricing table shape change.
PRICING_VERSION = "openai-sync-2026-08"

# Synchronous (non-batch) USD per 1M tokens.
# Cached input is reported separately for diagnostics but priced at the same
# input rate unless a model entry defines cached_input_per_million.
OPENAI_SYNC_PRICES_USD_PER_MILLION: dict[str, dict[str, float]] = {
    "gpt-5-mini": {
        "input_per_million": 0.25,
        "output_per_million": 2.00,
    },
}


@dataclass(frozen=True)
class TokenCosts:
    input_cost_usd: float | None
    output_cost_usd: float | None
    total_cost_usd: float | None
    priced: bool


def lookup_model_rates(model: str) -> dict[str, float] | None:
    """Return per-million USD rates for a known model, else None."""
    return OPENAI_SYNC_PRICES_USD_PER_MILLION.get(model)


def estimate_token_costs(
    *,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> TokenCosts:
    """Estimate USD costs from billed token totals.

    ``output_tokens`` is the API billed output total. Reasoning tokens are a
    breakdown of that total and must not be subtracted before pricing.
    Unknown models return NULL costs rather than guessing.
    """
    rates = lookup_model_rates(model)
    if rates is None:
        return TokenCosts(
            input_cost_usd=None,
            output_cost_usd=None,
            total_cost_usd=None,
            priced=False,
        )
    if input_tokens is None and output_tokens is None:
        return TokenCosts(
            input_cost_usd=None,
            output_cost_usd=None,
            total_cost_usd=None,
            priced=True,
        )
    input_cost = (
        None
        if input_tokens is None
        else (input_tokens / 1_000_000.0) * float(rates["input_per_million"])
    )
    output_cost = (
        None
        if output_tokens is None
        else (output_tokens / 1_000_000.0) * float(rates["output_per_million"])
    )
    if input_cost is None and output_cost is None:
        total = None
    else:
        total = (input_cost or 0.0) + (output_cost or 0.0)
    return TokenCosts(
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
        total_cost_usd=total,
        priced=True,
    )


def pricing_manifest() -> dict[str, Any]:
    return {
        "pricing_version": PRICING_VERSION,
        "openai_sync_usd_per_million": dict(OPENAI_SYNC_PRICES_USD_PER_MILLION),
    }
