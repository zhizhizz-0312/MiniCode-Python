"""Token pricing, so a run's cost can be reported instead of guessed at.

Upstream estimates cost as ``total_tokens * 0.000015``: one flat rate for everything.
That is adequate for a rate controller and wrong for a per-run report, because it charges
the same for a cached input token, a fresh input token and an output token, and it ignores
that the provider doubles its prices during peak hours.

The published table (CNY per million tokens):

===========  ==========  ==========
             off-peak    peak
===========  ==========  ==========
input cache  0.02        0.04
input fresh  1.00        2.00
output       4.00        8.00
===========  ==========  ==========

**Peak** is Beijing time, Monday to Friday excluding Chinese statutory holidays, 09:00-12:00
and 14:00-18:00.  Everything else -- evenings, nights, weekends, holidays -- is off-peak,
and off-peak is exactly half of peak.

Two honest limits, stated here rather than discovered later:

* the amounts are **CNY**, and the rest of the project mixes a `$` estimate from upstream;
  a report must therefore say which unit it is showing instead of printing a bare number;
* the turn runner exposes a *context* token count, not the input/output/cache split, so a
  figure derived from it is an estimate and a lower bound.  It is labelled as an estimate
  wherever it is printed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = [
    "BEIJING",
    "DEFAULT_TABLE",
    "PriceTable",
    "estimate_cost",
    "estimate_tokens",
    "basis",
    "format_cny",
    "is_peak",
]

#: Characters per token for the content estimate.  English prose runs about four; Chinese
#: is nearer one, so the divisor takes the pessimistic end and the result is called an
#: estimate everywhere it appears.
CHARS_PER_TOKEN = 4


def estimate_tokens(*texts: str) -> int:
    """Estimate tokens from text length, as a **floor** rather than a figure.

    Real token accounting is not reachable from outside the turn runner: the runtime
    events carry phase and stop metadata only, the turn returns plain messages, and the
    model adapter keeps no usage from the provider.  This estimate counts the *visible
    conversation* -- the instruction and the reply -- and therefore excludes the system
    prompt, the tool schemas and the whole context re-sent on every step, which together
    dominate the bill.  It exists so a cost column is not simply zero; it must never be
    read as "what this cost".
    """
    total = sum(len(text or "") for text in texts)
    return total // CHARS_PER_TOKEN

#: The provider prices in Beijing time, so the peak windows are evaluated there regardless
#: of where the agent runs.  A fixed offset is used rather than a timezone database: China
#: has observed a single offset since 1991, and a dependency would not earn its keep.
BEIJING = timezone(timedelta(hours=8))

_PEAK_WINDOWS = ((9, 12), (14, 18))


@dataclass(frozen=True, slots=True)
class PriceTable:
    """CNY per million tokens."""

    input_cached: float = 0.02
    input_fresh: float = 1.0
    output: float = 4.0
    peak_multiplier: float = 2.0

    def rates(self, *, peak: bool) -> tuple[float, float, float]:
        factor = self.peak_multiplier if peak else 1.0
        return (
            self.input_cached * factor,
            self.input_fresh * factor,
            self.output * factor,
        )


DEFAULT_TABLE = PriceTable()


def is_peak(moment: datetime | None = None) -> bool:
    """Is the provider charging peak rates at ``moment``?

    Weekends are never peak.  Chinese statutory holidays are not modelled -- the calendar
    changes yearly and a wrong guess is worse than a stated limitation, so a holiday is
    charged as if it were a working day: an over-estimate on a handful of days.
    """
    now = (moment or datetime.now(timezone.utc)).astimezone(BEIJING)
    if now.weekday() >= 5:  # Saturday, Sunday
        return False
    return any(start <= now.hour < end for start, end in _PEAK_WINDOWS)


def estimate_cost(
    *,
    input_cached_tokens: int = 0,
    input_fresh_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    moment: datetime | None = None,
    table: PriceTable = DEFAULT_TABLE,
) -> float:
    """Estimated cost in CNY.

    ``total_tokens`` exists for callers that only have a context size to work with -- the
    turn runner reports exactly that and no split.  Charging a whole context at the
    *fresh input* rate under-states the truth (output tokens cost four times as much) and
    never over-states it, which is the direction an estimate should err in when it is used
    to decide whether to keep spending.
    """
    cached_rate, fresh_rate, output_rate = table.rates(peak=is_peak(moment))
    fresh = max(0, int(input_fresh_tokens)) + max(0, int(total_tokens))
    return (
        max(0, int(input_cached_tokens)) * cached_rate
        + fresh * fresh_rate
        + max(0, int(output_tokens)) * output_rate
    ) / 1_000_000


def basis(moment: datetime | None = None, table: PriceTable = DEFAULT_TABLE) -> str:
    """One phrase naming the rate a figure was computed at.

    A number whose basis is invisible cannot be audited -- the reader cannot tell an
    off-peak cache-hit charge from a peak-rate output charge, and those differ by a factor
    of four hundred.
    """
    peak = is_peak(moment)
    cached, fresh, output = table.rates(peak=peak)
    window = "peak" if peak else "off-peak"
    return (
        f"{window} rate, CNY/1M: cache {cached:g}, fresh {fresh:g}, output {output:g}"
    )


def format_cny(amount: float, *, estimate: bool = True) -> str:
    """Render an amount with its unit, so a reader never has to guess the currency."""
    label = "~¥" if estimate else "¥"
    return f"{label}{amount:.4f}"
