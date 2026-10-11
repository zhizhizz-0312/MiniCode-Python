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

def test_the_currency_aware_calculator_answers_in_the_table_that_matched() -> None:
    from minicode.cost_tracker import calculate_cost_for

    amount, currency = calculate_cost_for("deepseek-v4-flash", input_tokens=1_000_000)
    usd_amount, usd_currency = calculate_cost_for("claude-sonnet-4-20250514", input_tokens=1_000_000)

    assert (amount, currency) == (1.0, "CNY")
    assert usd_currency == "USD" and usd_amount > 0


def test_the_currency_aware_calculator_applies_the_peak_multiplier() -> None:
    from minicode.cost_tracker import calculate_cost_for

    peak, _ = calculate_cost_for("deepseek-flash", input_tokens=1_000_000, peak=True)
    off, _ = calculate_cost_for("deepseek-flash", input_tokens=1_000_000, peak=False)

    assert (peak, off) == (2.0, 1.0)


def test_the_running_total_can_record_its_currency() -> None:
    """The internal account used to be implicitly USD while the model in use is priced in
    CNY -- one number with two possible meanings."""
    from minicode.state import AppState, add_cost

    state = AppState()
    assert state.cost_currency == "USD"
    state = add_cost(0.5, "CNY")(state)
    assert state.total_cost_usd == 0.5
    assert state.cost_currency == "CNY"


def test_adding_cost_without_a_currency_leaves_the_unit_alone() -> None:
    """Existing callers must behave exactly as before."""
    from minicode.state import AppState, add_cost

    state = add_cost(0.25)(AppState(cost_currency="CNY"))

    assert state.total_cost_usd == 0.25
    assert state.cost_currency == "CNY"
