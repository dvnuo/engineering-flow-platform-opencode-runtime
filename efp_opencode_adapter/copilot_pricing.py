"""Copilot AI Credits pricing for the models this runtime drives.

GitHub bills Copilot per token since 2026-06-01: input, cached input, cache
write and output tokens each have a USD price per million, the total is
converted to AI credits (1 credit = $0.01), and the GPT-6 line (plus the
GPT-5.6 line) has a "long context" tier that doubles the prices once one
request's prompt exceeds a threshold (272,000 input tokens for most of them,
200,000 for GPT-5.6 Luna). Reasoning tokens are output tokens.

The table mirrors GitHub Docs "Models and pricing for GitHub Copilot" as read
on 2026-10-27; OpenCode itself reports a cost of 0 for Copilot models
(models.dev has no price for a subscription provider), so this is what the
adapter uses to estimate credits. Kept free of package imports so the usage
tracker and the context API can both use it. The native runtime carries the
same table (``efp_runtime/llm/pricing.py``); keep them aligned.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

CREDIT_USD = 0.01
PRICING_SOURCE = "static"
DEFAULT_TIER = "default"
LONG_CONTEXT_TIER = "long_context"
GPT6_LONG_CONTEXT_THRESHOLD_TOKENS = 272_000
GPT56_LUNA_LONG_CONTEXT_THRESHOLD_TOKENS = 200_000


@dataclass(frozen=True)
class TierPrice:
    """USD per million tokens for one pricing tier."""

    input: float
    cached_input: float
    cache_write: float
    output: float


@dataclass(frozen=True)
class ModelPricing:
    model: str
    threshold_tokens: int | None
    default: TierPrice
    long_context: TierPrice | None = None

    def tier_for(self, prompt_tokens: int) -> str:
        if (
            self.long_context is not None
            and self.threshold_tokens is not None
            and prompt_tokens > self.threshold_tokens
        ):
            return LONG_CONTEXT_TIER
        return DEFAULT_TIER

    def price_for(self, tier: str) -> TierPrice:
        if tier == LONG_CONTEXT_TIER and self.long_context is not None:
            return self.long_context
        return self.default


def _gpt6(model: str, default: tuple[float, float, float, float], long: tuple[float, float, float, float], *, threshold: int = GPT6_LONG_CONTEXT_THRESHOLD_TOKENS) -> ModelPricing:
    return ModelPricing(
        model=model,
        threshold_tokens=threshold,
        default=TierPrice(*default),
        long_context=TierPrice(*long),
    )


# (input, cached input, cache write, output) USD per 1M tokens.
COPILOT_MODEL_PRICING: dict[str, ModelPricing] = {
    "gpt-6.1-sol": _gpt6("gpt-6.1-sol", (2.00, 0.10, 2.50, 10.00), (4.00, 0.20, 5.00, 15.00)),
    "gpt-6-sol": _gpt6("gpt-6-sol", (2.00, 0.20, 2.50, 10.00), (4.00, 0.40, 5.00, 15.00)),
    "gpt-6-luna": _gpt6("gpt-6-luna", (0.10, 0.01, 0.125, 0.50), (0.20, 0.02, 0.25, 0.75)),
    "gpt-6-astra": _gpt6("gpt-6-astra", (10.00, 1.00, 12.50, 50.00), (20.00, 2.00, 25.00, 75.00)),
    "gpt-6.1-terra": _gpt6("gpt-6.1-terra", (1.00, 0.05, 1.25, 5.00), (2.00, 0.10, 2.50, 7.50)),
    "gpt-5.6-sol": _gpt6("gpt-5.6-sol", (4.00, 0.40, 5.00, 20.00), (8.00, 0.80, 10.00, 30.00)),
    "gpt-5.6-terra": _gpt6("gpt-5.6-terra", (2.00, 0.20, 2.50, 12.00), (4.00, 0.40, 5.00, 18.00)),
    "gpt-5.6-luna": _gpt6(
        "gpt-5.6-luna",
        (0.20, 0.02, 0.25, 1.20),
        (0.40, 0.04, 0.50, 1.80),
        threshold=GPT56_LUNA_LONG_CONTEXT_THRESHOLD_TOKENS,
    ),
}


def normalize_pricing_model_id(value: Any) -> str | None:
    """``gpt-6.1-sol`` from ``GitHub-Copilot/GPT-6.1-Sol`` and the like; None for a non-string."""

    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if "/" in text:
        text = text.split("/", 1)[1]
    text = "-".join(text.split())
    return text or None


def pricing_for(model: Any) -> ModelPricing | None:
    model_id = normalize_pricing_model_id(model)
    return COPILOT_MODEL_PRICING.get(model_id) if model_id else None


def billing_threshold_tokens(model: Any) -> int | None:
    """The prompt size (input tokens per request) above which the long-context tier bills, or None."""

    pricing = pricing_for(model)
    if pricing is None or pricing.long_context is None:
        return None
    return pricing.threshold_tokens


def pricing_tier(model: Any, prompt_tokens: Any) -> str | None:
    """``default`` or ``long_context`` for one request of ``prompt_tokens`` input tokens; None for an unknown model."""

    pricing = pricing_for(model)
    if pricing is None:
        return None
    return pricing.tier_for(_count(prompt_tokens))


def _count(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def usd_for(
    model: Any,
    *,
    input_tokens: Any = 0,
    cached_input_tokens: Any = 0,
    cache_write_tokens: Any = 0,
    output_tokens: Any = 0,
    reasoning_tokens: Any = 0,
    tier: str | None = None,
) -> float | None:
    """USD for one request (or one run's totals) at the model's published rates; None for an unknown model.

    ``input_tokens`` is the whole prompt (the OpenAI convention: cached and
    cache-write tokens are part of it); cached tokens bill at the cached
    price, cache-write tokens at the cache-write price instead of the input
    price, the rest at the input price. Reasoning tokens are output tokens.
    The tier is the request's own unless the caller names one (a run's
    totals are not one prompt, so the caller passes the tier of its largest
    request).
    """

    pricing = pricing_for(model)
    if pricing is None:
        return None
    prompt = _count(input_tokens)
    cached = min(_count(cached_input_tokens), prompt)
    written = min(_count(cache_write_tokens), max(0, prompt - cached))
    uncached = max(0, prompt - cached - written)
    output = _count(output_tokens) + _count(reasoning_tokens)
    price = pricing.price_for(tier or pricing.tier_for(prompt))
    usd = (
        uncached * price.input
        + cached * price.cached_input
        + written * price.cache_write
        + output * price.output
    ) / 1_000_000
    return round(usd, 6)


def credits_for(
    model: Any,
    *,
    input_tokens: Any = 0,
    cached_input_tokens: Any = 0,
    cache_write_tokens: Any = 0,
    output_tokens: Any = 0,
    reasoning_tokens: Any = 0,
    tier: str | None = None,
) -> float | None:
    """AI credits (1 credit = $0.01) for the tokens given; None for a model the table does not know."""

    usd = usd_for(
        model,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        tier=tier,
    )
    if usd is None:
        return None
    return round(usd / CREDIT_USD, 2)


def pricing_payload(model: Any, *, tier: str | None) -> dict[str, Any]:
    """The ``pricing`` block the Portal reads next to ``estimated_credits``."""

    pricing = pricing_for(model)
    return {
        "model": normalize_pricing_model_id(model),
        "tier": tier if pricing is not None else None,
        "source": PRICING_SOURCE,
        "threshold_tokens": pricing.threshold_tokens if pricing is not None and pricing.long_context is not None else None,
    }


def estimate_usage_credits(model: Any, usage: Mapping[str, Any], *, largest_prompt_tokens: Any = None) -> dict[str, Any]:
    """``{"estimated_credits", "pricing", "cache_hit_ratio"}`` for a usage row.

    ``usage`` carries ``input_tokens`` (whole prompt), ``cached_input_tokens``,
    ``cache_write_tokens``, ``output_tokens`` and ``reasoning_tokens``. The
    tier is judged on ``largest_prompt_tokens`` (the biggest single request of
    a run) when given, else on ``input_tokens`` as if it were one request.
    """

    prompt = _count(usage.get("input_tokens"))
    cached = _count(usage.get("cached_input_tokens"))
    tier_prompt = _count(largest_prompt_tokens) if largest_prompt_tokens is not None else prompt
    tier = pricing_tier(model, tier_prompt)
    credits = credits_for(
        model,
        input_tokens=prompt,
        cached_input_tokens=cached,
        cache_write_tokens=usage.get("cache_write_tokens"),
        output_tokens=usage.get("output_tokens"),
        reasoning_tokens=usage.get("reasoning_tokens"),
        tier=tier,
    )
    return {
        "estimated_credits": credits,
        "pricing": pricing_payload(model, tier=tier),
        "cache_hit_ratio": round(min(cached, prompt) / prompt, 4) if prompt > 0 else None,
    }


__all__ = [
    "COPILOT_MODEL_PRICING",
    "CREDIT_USD",
    "DEFAULT_TIER",
    "GPT6_LONG_CONTEXT_THRESHOLD_TOKENS",
    "LONG_CONTEXT_TIER",
    "PRICING_SOURCE",
    "ModelPricing",
    "TierPrice",
    "billing_threshold_tokens",
    "credits_for",
    "estimate_usage_credits",
    "normalize_pricing_model_id",
    "pricing_for",
    "pricing_payload",
    "pricing_tier",
    "usd_for",
]
