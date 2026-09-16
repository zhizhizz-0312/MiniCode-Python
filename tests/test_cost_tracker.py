"""Regression tests for minicode.cost_tracker.

Covers the Decimal/float TypeError bug in calculate_cost(): the standalone
cost helper divided token counts by a Decimal constant and multiplied the
result by float pricing values, raising
``TypeError: unsupported operand type(s) for *: 'decimal.Decimal' and 'float'``
on every successful model response tracked through a state store (the TUI
path). CostTracker.add_usage used plain float math and was unaffected.

Also covers MODEL_PRICING entries for the DeepSeek models used via the
Anthropic-compatible channel (previously fell back to the 30x-overstated
"default" pricing).
"""
from __future__ import annotations

import math

from minicode.cost_tracker import MODEL_PRICING, CostTracker, calculate_cost


def test_calculate_cost_unknown_model_returns_float() -> None:
    """Regression: this call raised TypeError (Decimal * float) before the fix."""
    cost = calculate_cost("totally-unknown-model", input_tokens=1_000, output_tokens=500)
    assert isinstance(cost, float)
    expected = (1_000 / 1_000_000) * 3.0 + (500 / 1_000_000) * 15.0
    assert math.isclose(cost, expected)


def test_calculate_cost_zero_tokens_is_zero() -> None:
    cost = calculate_cost("any-model")
    assert cost == 0.0


def test_calculate_cost_deepseek_v4_pro_uses_registered_pricing() -> None:
    """1M input + 1M output tokens must cost 0.10 + 0.40 USD, not the default 3/15."""
    assert "deepseek-v4-pro" in MODEL_PRICING
    cost = calculate_cost(
        "deepseek-v4-pro", input_tokens=1_000_000, output_tokens=1_000_000
    )
    pricing = MODEL_PRICING["deepseek-v4-pro"]
    assert math.isclose(cost, pricing["input"] + pricing["output"])
    assert math.isclose(pricing["input"], 0.10)
    assert math.isclose(pricing["output"], 0.40)


def test_calculate_cost_deepseek_flash_uses_registered_pricing() -> None:
    assert "deepseek-flash" in MODEL_PRICING
    cost = calculate_cost(
        "deepseek-flash", input_tokens=1_000_000, output_tokens=1_000_000
    )
    pricing = MODEL_PRICING["deepseek-flash"]
    assert math.isclose(cost, pricing["input"] + pricing["output"])
    # flash must price below v4-pro on both directions
    pro = MODEL_PRICING["deepseek-v4-pro"]
    assert pricing["input"] < pro["input"]
    assert pricing["output"] < pro["output"]


def test_calculate_cost_deepseek_v4_flash_canonical_id_priced() -> None:
    """The canonical id (used as the configured default model) must not fall
    back to the 30x-overstated default pricing."""
    assert "deepseek-v4-flash" in MODEL_PRICING
    cost = calculate_cost(
        "deepseek-v4-flash", input_tokens=1_000_000, output_tokens=1_000_000
    )
    pricing = MODEL_PRICING["deepseek-v4-flash"]
    assert math.isclose(cost, pricing["input"] + pricing["output"])
    # same tier as the short spelling listed by GET /models
    assert pricing == MODEL_PRICING["deepseek-flash"]


def test_calculate_cost_cache_tokens_included() -> None:
    pricing = MODEL_PRICING["deepseek-v4-pro"]
    cost = calculate_cost(
        "deepseek-v4-pro",
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    )
    assert math.isclose(cost, pricing["cache_read"] + pricing["cache_write"])


def test_add_usage_tracks_totals_with_float_cost() -> None:
    tracker = CostTracker()
    cost = tracker.add_usage(
        "deepseek-v4-pro", input_tokens=1_000, output_tokens=500, duration_ms=10
    )
    assert isinstance(cost, float)
    usage = tracker.get_model_usage("deepseek-v4-pro")
    assert usage.call_count == 1
    assert usage.input_tokens == 1_000
    assert usage.output_tokens == 500
    assert usage.total_duration_ms == 10
    assert usage.cost_usd > 0
    assert tracker.total_cost_usd > 0
    assert tracker.get_total_tokens() == 1_500
