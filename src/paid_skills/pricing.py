"""Pricing model for paid Claude Skills.

Simple, transparent pricing: per-invocation fee + per-1k-token pass-through.
Creators set ``price_per_call`` and (optionally) ``price_per_1k_tokens``. The
runtime uses these to produce a quote before invocation. Stripe / Lightning /
USDC adapters (out of scope for the OSS core) hook into ``PaymentHook`` below.

This module is intentionally framework-free: pricing is pure math, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceQuote:
    """A price quote returned by ``calculate_price``.

    Attributes:
        per_call_usd: Flat fee charged per invocation, in USD.
        token_cost_usd: Pass-through cost of input+output tokens (USD).
        total_usd: Sum of the two.
        input_tokens: Tokens used for input.
        output_tokens: Tokens used for output.
        cost_per_1k_tokens_usd: Pass-through rate used.
    """

    per_call_usd: float
    token_cost_usd: float
    total_usd: float
    input_tokens: int
    output_tokens: int
    cost_per_1k_tokens_usd: float


def calculate_price(
    *,
    input_tokens: int,
    output_tokens: int,
    cost_per_1k_tokens_usd: float = 0.018,
    per_call_usd: float = 0.01,
    margin: float = 0.30,
) -> PriceQuote:
    """Compute a price quote for a skill invocation.

    Args:
        input_tokens: Number of input tokens that will be sent.
        output_tokens: Expected output tokens (use 0 for unknown, then meter after).
        cost_per_1k_tokens_usd: Anthropic model cost per 1K tokens (USD).
            Default 0.018 ≈ Sonnet blended rate. Set to actual model rate.
        per_call_usd: Flat per-call fee charged on top of token cost.
        margin: Markup on token cost, applied as multiplier (0.30 = +30%).

    Returns:
        A ``PriceQuote`` with the breakdown.
    """
    raw_token_cost = (input_tokens + output_tokens) / 1000.0 * cost_per_1k_tokens_usd
    token_cost = raw_token_cost * (1.0 + margin)
    total = per_call_usd + token_cost
    return PriceQuote(
        per_call_usd=round(per_call_usd, 4),
        token_cost_usd=round(token_cost, 6),
        total_usd=round(total, 6),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_per_1k_tokens_usd=cost_per_1k_tokens_usd,
    )


def format_price(quote: PriceQuote) -> str:
    """Render a PriceQuote for display (e.g. in API responses or receipts)."""
    return (
        f"${quote.total_usd:.4f} "
        f"(${quote.per_call_usd:.4f} per-call + "
        f"${quote.token_cost_usd:.4f} tokens @ "
        f"${quote.cost_per_1k_tokens_usd:.3f}/1k × {quote.input_tokens + quote.output_tokens} tokens)"
    )