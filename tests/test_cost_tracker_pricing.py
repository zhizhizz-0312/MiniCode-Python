"""Tests for the currency-aware price lookup.

The point of returning the currency alongside the rates is that a caller cannot silently
add a USD figure to a CNY one -- the failure this project has already found twice in other
guises.
"""

from __future__ import annotations

from minicode.cost_tracker import MODEL_PRICING, MODEL_PRICING_CNY, calculate_cost, pricing_for


def test_a_deepseek_model_is_priced_in_cny_from_the_published_table() -> None:
    rates, currency = pricing_for("deepseek-v4-flash")

    assert currency == "CNY"
    assert rates["input"] == 1.0
    assert rates["output"] == 4.0
    assert rates["cache_read"] == 0.02
    assert rates["peak_multiplier"] == 2.0


def test_the_known_variants_all_hit_the_same_published_rates() -> None:
    for name in ("deepseek-flash", "deepseek-v4-flash", "DeepSeek"):
        rates, currency = pricing_for(name)
        assert currency == "CNY"
        assert rates is MODEL_PRICING_CNY["deepseek"]


def test_an_unknown_model_falls_back_to_cny_not_to_usd() -> None:
    """A wrong-currency default is worse than a conservative one."""
    _rates, currency = pricing_for("some-unreleased-model")

    assert currency == "CNY"


def test_the_existing_usd_path_is_untouched() -> None:
    """The addition must not change what the project already computes."""
    cost = calculate_cost("claude-sonnet-4-20250514", input_tokens=1_000_000)

    assert cost == MODEL_PRICING.get("claude-sonnet-4-20250514", MODEL_PRICING["default"])["input"]


def test_the_two_tables_are_separate_and_labelled() -> None:
    """One dict holding two currencies is the mistake this split exists to prevent."""
    assert "deepseek" not in MODEL_PRICING
    assert MODEL_PRICING_CNY["deepseek"]["input"] != MODEL_PRICING["default"]["input"]
