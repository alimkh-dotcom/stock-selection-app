"""Event stacking: does chatter arrive before a price move, or after?

This is the gate. People usually post about a stock *because* it jumped, and if
that is all that happens then the chatter is a rear-view mirror and there is no
strategy to build. The question is cheap to answer and worth answering first.

The method is the one used to pull faint signals out of noisy recordings: find
every moment chatter spiked, line them all up at day zero, and average across
thousands of them. Noise in each event points a different way and cancels;
whatever is common to all of them survives.

Reading the result:

* returns rising **before** day zero -> Reddit is late, the move came first
* returns rising **after** day zero  -> chatter carried information

Two corrections matter enough to be built in rather than left to the caller.

**Market adjustment.** If the whole market rises, every stack looks bullish.
Each stock's return has the market's return subtracted, so only the part
specific to that company is measured.

**Clustered errors.** Attention spikes are not independent: hundreds of stocks
spike on the same frantic day, and their returns move together. Treating each
event as an independent observation would shrink the error bars dramatically and
manufacture significance out of one exciting week. Events are therefore averaged
within a calendar date first, and the spread taken across dates.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping, Sequence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttentionEvent:
    symbol: str
    day: date
    zscore: float
    authors: int


@dataclass
class StackResult:
    """The averaged path around the event, in market-adjusted daily returns."""

    offsets: list[int]
    mean_return: list[float]
    stderr: list[float]
    n_events: int
    n_dates: int
    coverage: list[int] = field(default_factory=list)

    @property
    def cumulative(self) -> list[float]:
        """Cumulative market-adjusted return across the window."""
        total, out = 0.0, []
        for r in self.mean_return:
            total += r
            out.append(total)
        return out

    def drift(self, lo: int, hi: int) -> float:
        """Summed mean return over offsets in [lo, hi]."""
        return sum(
            r for off, r in zip(self.offsets, self.mean_return) if lo <= off <= hi
        )

    def verdict(self, before: int, after: int) -> str:
        """A blunt read of which side of day zero the move sits on."""
        pre, post = self.drift(-before, -1), self.drift(1, after)
        if abs(pre) < 1e-9 and abs(post) < 1e-9:
            return "no movement either side"
        if post > abs(pre):
            return "chatter leads price"
        if pre > abs(post):
            return "chatter follows price"
        return "mixed"


def rolling_zscores(
    counts_by_day: Mapping[date, float],
    baseline_days: int = 30,
    min_baseline: int = 10,
) -> dict[date, float]:
    """How unusual each day's chatter is versus that symbol's own recent normal.

    Comparing a symbol to itself rather than to other symbols is deliberate:
    absolute counts would simply rank mega caps first every day, and the forum
    grew from niche to millions of members over the study period, so any measure
    that is not relative would mostly track that growth.

    Only days strictly before the day in question enter its baseline.
    """
    days = sorted(counts_by_day)
    out: dict[date, float] = {}
    for i, day in enumerate(days):
        window = [counts_by_day[d] for d in days[max(0, i - baseline_days) : i]]
        if len(window) < min_baseline:
            continue
        mean = statistics.fmean(window)
        sd = statistics.pstdev(window)
        if sd <= 0:
            # A symbol that has never been mentioned before is genuinely novel,
            # but with no spread there is no scale on which to call it unusual.
            out[day] = 0.0 if counts_by_day[day] <= mean else float("inf")
            continue
        out[day] = (counts_by_day[day] - mean) / sd
    return out


def find_events(
    panel: Mapping[str, Mapping[date, float]],
    threshold: float = 2.0,
    min_authors: int = 5,
    baseline_days: int = 30,
) -> list[AttentionEvent]:
    """Find attention spikes across every symbol.

    ``min_authors`` is a floor on *distinct people*, not mentions. One person
    posting fifty times is one opinion, and a floor on raw mentions would let a
    single obsessive or a small coordinated group manufacture events.
    """
    events: list[AttentionEvent] = []
    for symbol, counts in panel.items():
        zs = rolling_zscores(counts, baseline_days)
        for day, z in zs.items():
            if z >= threshold and counts[day] >= min_authors:
                events.append(AttentionEvent(symbol, day, z, int(counts[day])))
    events.sort(key=lambda e: (e.day, e.symbol))
    return events


def stack_events(
    events: Sequence[AttentionEvent],
    returns_for: "ReturnsLookup",
    before: int = 20,
    after: int = 20,
    min_dates: int = 5,
) -> StackResult:
    """Average market-adjusted returns around every event.

    ``returns_for(symbol, day, before, after)`` must return one market-adjusted
    return per offset, using ``None`` where data is missing, so that a short
    history pads rather than shifting day zero.
    """
    offsets = list(range(-before, after + 1))
    # Group by date first: events on the same day share market conditions, so
    # they are one observation, not many.
    by_date: dict[date, list[list[float | None]]] = defaultdict(list)

    for event in events:
        path = returns_for(event.symbol, event.day, before, after)
        if path is None:
            continue
        by_date[event.day].append(path)

    date_means: dict[date, list[float | None]] = {}
    for day, paths in by_date.items():
        means: list[float | None] = []
        for i in range(len(offsets)):
            values = [p[i] for p in paths if p[i] is not None]
            means.append(statistics.fmean(values) if values else None)
        date_means[day] = means

    mean_return, stderr, coverage = [], [], []
    for i in range(len(offsets)):
        values = [m[i] for m in date_means.values() if m[i] is not None]
        coverage.append(len(values))
        if len(values) < min_dates:
            mean_return.append(0.0)
            stderr.append(float("nan"))
            continue
        mean_return.append(statistics.fmean(values))
        # A single date gives a mean but no spread; report the mean with an
        # undefined error rather than implying a precision we do not have.
        stderr.append(
            statistics.stdev(values) / math.sqrt(len(values))
            if len(values) >= 2
            else float("nan")
        )

    return StackResult(
        offsets=offsets,
        mean_return=mean_return,
        stderr=stderr,
        n_events=sum(len(p) for p in by_date.values()),
        n_dates=len(by_date),
        coverage=coverage,
    )


class ReturnsLookup:
    """Market-adjusted return paths around an event.

    Subtracting the market removes the component every stock shares, which is
    otherwise the single largest source of noise: on a day the whole market
    rallies, every stack looks like a winner.
    """

    def __init__(self, price_store, market_series, start: date, end: date) -> None:
        self.prices = price_store
        self.market = market_series
        self.start = start
        self.end = end
        self._cache: dict[str, object] = {}

    def _series(self, symbol: str):
        if symbol not in self._cache:
            self._cache[symbol] = self.prices.get(symbol, self.start, self.end)
        return self._cache[symbol]

    def __call__(
        self, symbol: str, day: date, before: int, after: int
    ) -> list[float | None] | None:
        series = self._series(symbol)
        if series is None:
            return None
        if not series.was_listed_on(day):
            # An anachronistic mention: the company was not public yet.
            return None
        stock = series.window_returns(day, before, after)
        market = self.market.window_returns(day, before, after)
        return [
            None if s is None or m is None else s - m
            for s, m in zip(stock, market)
        ]
