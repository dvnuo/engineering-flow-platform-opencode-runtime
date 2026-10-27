"""The Copilot AI credits price table and the credit estimate built on it."""

from efp_opencode_adapter.copilot_pricing import (
    COPILOT_MODEL_PRICING,
    billing_threshold_tokens,
    credits_for,
    estimate_usage_credits,
    normalize_pricing_model_id,
    pricing_for,
    pricing_payload,
    pricing_tier,
    usd_for,
)


def test_table_carries_the_published_gpt_6_rates():
    sol = COPILOT_MODEL_PRICING["gpt-6.1-sol"]
    assert (sol.default.input, sol.default.cached_input, sol.default.cache_write, sol.default.output) == (2.00, 0.10, 2.50, 10.00)
    assert (sol.long_context.input, sol.long_context.cached_input, sol.long_context.cache_write, sol.long_context.output) == (4.00, 0.20, 5.00, 15.00)
    assert sol.threshold_tokens == 272_000
    assert COPILOT_MODEL_PRICING["gpt-6-luna"].default.input == 0.10
    assert COPILOT_MODEL_PRICING["gpt-6-astra"].long_context.output == 75.00
    assert COPILOT_MODEL_PRICING["gpt-5.6-luna"].threshold_tokens == 200_000


def test_model_ids_are_accepted_with_or_without_the_provider_prefix():
    assert normalize_pricing_model_id("GitHub-Copilot/GPT-6.1-Sol") == "gpt-6.1-sol"
    assert normalize_pricing_model_id("  gpt-6 sol ") == "gpt-6-sol"
    assert normalize_pricing_model_id(None) is None
    assert pricing_for("github-copilot/gpt-6-luna") is COPILOT_MODEL_PRICING["gpt-6-luna"]
    assert pricing_for("claude-sonnet-5.5") is None
    assert billing_threshold_tokens("gpt-6-sol") == 272_000
    assert billing_threshold_tokens("mai-code-1.1-flash") is None


def test_tier_is_judged_on_one_requests_prompt_size():
    assert pricing_tier("gpt-6.1-sol", 272_000) == "default"
    assert pricing_tier("gpt-6.1-sol", 272_001) == "long_context"
    assert pricing_tier("gpt-5.6-luna", 200_001) == "long_context"
    assert pricing_tier("gpt-6.1-sol", "bad") == "default"
    assert pricing_tier("unknown-model", 10) is None


def test_credits_follow_the_published_rates_per_token_kind():
    # 100,000 uncached input tokens on GPT-6.1 Sol: $0.20 = 20 credits.
    assert credits_for("gpt-6.1-sol", input_tokens=100_000) == 20.0
    # The same prompt fully cached bills the cached price only.
    assert credits_for("gpt-6.1-sol", input_tokens=100_000, cached_input_tokens=100_000) == 1.0
    # Cache-write tokens replace the input price, they do not add to it.
    assert credits_for("gpt-6.1-sol", input_tokens=100_000, cache_write_tokens=100_000) == 25.0
    # One request of a million tokens is itself in the long-context tier.
    assert credits_for("gpt-6.1-sol", input_tokens=1_000_000) == 400.0
    # Reasoning is output.
    assert credits_for("gpt-6.1-sol", output_tokens=50_000, reasoning_tokens=50_000) == 100.0
    # Above the threshold every price doubles (Sol's cached price included).
    assert usd_for("gpt-6-sol", input_tokens=300_000) == 1.2
    assert credits_for("gpt-6-sol", input_tokens=300_000, cached_input_tokens=100_000) == 84.0
    # A run total is not one prompt: the caller names the tier of its largest request.
    assert credits_for("gpt-6-sol", input_tokens=300_000, tier="default") == 60.0
    assert credits_for("not-a-copilot-model", input_tokens=1_000_000) is None
    assert credits_for("gpt-6-luna", input_tokens=-5, output_tokens=None) == 0.0


def test_usage_estimate_reports_credits_tier_and_cache_ratio():
    estimate = estimate_usage_credits(
        "github-copilot/gpt-6.1-sol",
        {"input_tokens": 25_000, "cached_input_tokens": 20_000, "cache_write_tokens": 1_000, "output_tokens": 700, "reasoning_tokens": 300},
        largest_prompt_tokens=14_000,
    )
    # 4,000 uncached ($0.008) + 20,000 cached ($0.002) + 1,000 written ($0.0025) + 1,000 output ($0.01).
    assert estimate["estimated_credits"] == 2.25
    assert estimate["cache_hit_ratio"] == 0.8
    assert estimate["pricing"] == {"model": "gpt-6.1-sol", "tier": "default", "source": "static", "threshold_tokens": 272_000}

    long_run = estimate_usage_credits("gpt-6.1-sol", {"input_tokens": 300_000, "cached_input_tokens": 100_000}, largest_prompt_tokens=300_000)
    assert long_run["pricing"]["tier"] == "long_context"
    assert long_run["estimated_credits"] == 82.0

    unknown = estimate_usage_credits("claude-sonnet-5.5", {"input_tokens": 10})
    assert unknown["estimated_credits"] is None
    assert unknown["pricing"] == {"model": "claude-sonnet-5.5", "tier": None, "source": "static", "threshold_tokens": None}
    assert unknown["cache_hit_ratio"] == 0.0
    assert estimate_usage_credits("gpt-6-luna", {})["cache_hit_ratio"] is None
    assert pricing_payload(None, tier="default") == {"model": None, "tier": None, "source": "static", "threshold_tokens": None}
