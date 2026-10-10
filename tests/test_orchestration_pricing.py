"""Tests for the price model.

The published table has three asymmetries that a flat per-token rate cannot express, and
each one is pinned here: cache hits cost a fiftieth of fresh input, output costs four
times fresh input, and peak hours double everything.  Getting the peak window wrong is a
2x error on every estimate, so the boundaries are tested rather than assumed.
"""

from __future__ import annotations

from datetime import datetime

from minicode.orchestration.pricing import (
    BEIJING,
    DEFAULT_TABLE,
    PriceTable,
    estimate_cost,
    format_cny,
    is_peak,
)


def _at(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, 0, tzinfo=BEIJING)


def test_the_table_matches_the_published_rates() -> None:
    assert (DEFAULT_TABLE.input_cached, DEFAULT_TABLE.input_fresh, DEFAULT_TABLE.output) == (
        0.02, 1.0, 4.0,
    )
    assert DEFAULT_TABLE.peak_multiplier == 2.0


def test_cache_hits_cost_a_fiftieth_of_fresh_input() -> None:
    cached = estimate_cost(input_cached_tokens=1_000_000, moment=_at(2026, 10, 10, 22))
    fresh = estimate_cost(input_fresh_tokens=1_000_000, moment=_at(2026, 10, 10, 22))

    assert cached == 0.02
    assert fresh == 1.0


def test_output_costs_four_times_fresh_input() -> None:
    assert estimate_cost(output_tokens=1_000_000, moment=_at(2026, 10, 10, 22)) == 4.0


def test_peak_doubles_every_rate() -> None:
    off = estimate_cost(input_fresh_tokens=1_000_000, moment=_at(2026, 10, 9, 10))  # Friday 10:00
    assert off == 2.0  # peak
    evening = estimate_cost(input_fresh_tokens=1_000_000, moment=_at(2026, 10, 9, 20))
    assert evening == 1.0  # off-peak


def test_peak_windows_are_the_two_morning_and_afternoon_blocks() -> None:
    assert is_peak(_at(2026, 10, 9, 9)) is True
    assert is_peak(_at(2026, 10, 9, 11)) is True
    assert is_peak(_at(2026, 10, 9, 12)) is False  # 12:00 is the boundary, exclusive
    assert is_peak(_at(2026, 10, 9, 14)) is True
    assert is_peak(_at(2026, 10, 9, 17)) is True
    assert is_peak(_at(2026, 10, 9, 18)) is False
    assert is_peak(_at(2026, 10, 9, 8)) is False


def test_weekends_are_never_peak() -> None:
    assert is_peak(_at(2026, 10, 10, 10)) is False  # Saturday
    assert is_peak(_at(2026, 10, 11, 15)) is False  # Sunday


def test_a_whole_context_charged_at_the_fresh_rate_understates_output() -> None:
    """The direction of the error matters: an estimate must not flatter the spend."""
    total = estimate_cost(total_tokens=1_000_000, moment=_at(2026, 10, 10, 22))
    as_output = estimate_cost(output_tokens=1_000_000, moment=_at(2026, 10, 10, 22))

    assert total == 1.0
    assert total <= as_output


def test_costs_are_rendered_with_their_unit_and_estimate_marker() -> None:
    assert format_cny(0.0123) == "~¥0.0123"
    assert format_cny(1.5, estimate=False) == "¥1.5000"


def test_a_custom_table_is_honoured() -> None:
    table = PriceTable(input_fresh=10.0, peak_multiplier=1.0)

    assert estimate_cost(input_fresh_tokens=1_000_000, moment=_at(2026, 10, 9, 10), table=table) == 10.0

def test_the_basis_names_the_window_and_every_rate() -> None:
    """A figure whose basis is invisible cannot be audited: off-peak cache and peak output
    differ by a factor of four hundred."""
    from minicode.orchestration.pricing import basis

    peak = basis(_at(2026, 10, 9, 10))
    off = basis(_at(2026, 10, 9, 22))

    assert "peak rate" in peak and "0.04" in peak and "8" in peak
    assert "off-peak" in off and "0.02" in off and "4" in off

def test_the_single_source_lookup_prices_a_deepseek_model_in_cny() -> None:
    """The rates come from the project's own table, not from a second copy of them."""
    from minicode.orchestration.pricing import estimate_cost_for_model

    amount, currency = estimate_cost_for_model(
        "deepseek-v4-flash", input_fresh_tokens=1_000_000, moment=_at(2026, 10, 10, 22)
    )

    assert currency == "CNY"
    assert amount == 1.0


def test_the_single_source_lookup_applies_the_peak_multiplier() -> None:
    from minicode.orchestration.pricing import estimate_cost_for_model

    peak, _ = estimate_cost_for_model(
        "deepseek-flash", input_fresh_tokens=1_000_000, moment=_at(2026, 10, 9, 10)
    )
    off, _ = estimate_cost_for_model(
        "deepseek-flash", input_fresh_tokens=1_000_000, moment=_at(2026, 10, 9, 22)
    )

    assert peak == 2.0
    assert off == 1.0


def test_the_single_source_lookup_reports_usd_for_a_usd_model() -> None:
    """Rather than relabelling a dollar figure as yuan."""
    from minicode.cost_tracker import MODEL_PRICING
    from minicode.orchestration.pricing import estimate_cost_for_model

    usd_model = next(name for name in MODEL_PRICING if name != "default")
    amount, currency = estimate_cost_for_model(usd_model, input_fresh_tokens=1_000_000)

    assert currency == "USD"
    assert amount == MODEL_PRICING[usd_model]["input"]


def test_an_unknown_model_is_priced_in_the_conservative_currency() -> None:
    from minicode.orchestration.pricing import estimate_cost_for_model

    _amount, currency = estimate_cost_for_model("who-knows", input_fresh_tokens=10)

    assert currency == "CNY"
