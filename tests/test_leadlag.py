"""Lead/lag tests.

The important ones plant a known answer in synthetic data and check the
machinery recovers it. An event study that cannot find a signal it was handed
tells us nothing when pointed at real data -- and a null result from it would be
indistinguishable from a bug.
"""

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.leadlag import (
    AttentionEvent,
    find_events,
    rolling_zscores,
    stack_events,
)


# --- z-scores --------------------------------------------------------------

def days(n, start=date(2024, 1, 1)):
    return [start + timedelta(days=i) for i in range(n)]


def test_zscore_uses_only_earlier_days():
    """A day's baseline must not include itself or anything after it."""
    d = days(40)
    counts = {day: 1.0 for day in d}
    counts[d[35]] = 100.0
    zs = rolling_zscores(counts, baseline_days=30)
    assert zs[d[35]] > 5
    # The spike must not inflate the baseline of days that came before it.
    assert zs[d[34]] == pytest.approx(0.0, abs=1e-6)


def test_days_without_enough_history_are_skipped():
    counts = {day: 1.0 for day in days(5)}
    assert rolling_zscores(counts, baseline_days=30, min_baseline=10) == {}


def test_flat_history_then_first_mention_is_infinite_not_a_crash():
    d = days(40)
    counts = {day: 0.0 for day in d}
    counts[d[35]] = 20.0
    assert rolling_zscores(counts)[d[35]] == float("inf")


# --- event detection -------------------------------------------------------

def test_events_need_enough_distinct_people():
    """One obsessive posting fifty times is one opinion, not an event."""
    d = days(40)
    counts = {day: 0.5 for day in d}
    counts[d[35]] = 4.0            # a big z-score, but only 4 people
    events = find_events({"AAA": counts}, threshold=2.0, min_authors=5)
    assert events == []


def test_event_detected_when_both_thresholds_met():
    d = days(40)
    counts = {day: 1.0 for day in d}
    counts[d[35]] = 20.0
    events = find_events({"AAA": counts}, threshold=2.0, min_authors=5)
    assert [(e.symbol, e.day) for e in events] == [("AAA", d[35])]


# --- the planted-signal tests ---------------------------------------------

def make_lookup(paths):
    """Return a lookup serving pre-built return paths per (symbol, day)."""
    def lookup(symbol, day, before, after):
        return paths.get((symbol, day))
    return lookup


def build_events_and_paths(n_events, before, after, jump_offset, jump_size, seed=1):
    """Events whose only real move is a jump at ``jump_offset``, plus noise."""
    rng = random.Random(seed)
    events, paths = [], {}
    width = before + after + 1
    for i in range(n_events):
        # Spread events across distinct dates: clustered errors group by date,
        # so all-same-day events would collapse to a single observation.
        day = date(2024, 1, 1) + timedelta(days=i)
        symbol = f"S{i}"
        path = [rng.gauss(0, 0.01) for _ in range(width)]
        path[before + jump_offset] += jump_size
        paths[(symbol, day)] = path
        events.append(AttentionEvent(symbol, day, 3.0, 10))
    return events, paths


def test_recovers_a_planted_lead():
    """Jump three days AFTER the spike: chatter carried information."""
    before = after = 10
    events, paths = build_events_and_paths(200, before, after, jump_offset=+3,
                                           jump_size=0.05)
    result = stack_events(events, make_lookup(paths), before, after)

    assert result.n_events == 200
    assert result.mean_return[before + 3] == pytest.approx(0.05, abs=0.01)
    assert result.drift(1, after) > 0.04
    assert abs(result.drift(-before, -1)) < 0.01
    assert result.verdict(before, after) == "chatter leads price"


def test_recovers_a_planted_lag():
    """Jump three days BEFORE the spike: Reddit is reacting, not predicting."""
    before = after = 10
    events, paths = build_events_and_paths(200, before, after, jump_offset=-3,
                                           jump_size=0.05)
    result = stack_events(events, make_lookup(paths), before, after)

    assert result.drift(-before, -1) > 0.04
    assert abs(result.drift(1, after)) < 0.01
    assert result.verdict(before, after) == "chatter follows price"


def test_pure_noise_yields_no_verdict_either_way():
    """The null case must look null -- otherwise every result is suspect."""
    before = after = 10
    events, paths = build_events_and_paths(400, before, after, jump_offset=0,
                                           jump_size=0.0, seed=99)
    result = stack_events(events, make_lookup(paths), before, after)
    assert abs(result.drift(-before, -1)) < 0.01
    assert abs(result.drift(1, after)) < 0.01


# --- statistical discipline ------------------------------------------------

def test_same_day_events_count_as_one_observation():
    """Hundreds of stocks spike together on frantic days and move together.

    Counting each as independent would shrink the error bars and manufacture
    significance out of a single exciting week.
    """
    before = after = 5
    day = date(2024, 6, 3)
    paths = {(f"S{i}", day): [0.02] * (before + after + 1) for i in range(100)}
    events = [AttentionEvent(f"S{i}", day, 3.0, 10) for i in range(100)]
    result = stack_events(events, make_lookup(paths), before, after, min_dates=1)

    assert result.n_events == 100
    assert result.n_dates == 1, "same-day events were treated as independent"


def test_missing_data_pads_rather_than_shifting_day_zero():
    before = after = 3
    width = before + after + 1
    paths = {(f"S{i}", date(2024, 1, 1) + timedelta(days=i)):
             [None, None] + [0.01] * (width - 2) for i in range(20)}
    events = [AttentionEvent(f"S{i}", date(2024, 1, 1) + timedelta(days=i), 3.0, 10)
              for i in range(20)]
    result = stack_events(events, make_lookup(paths), before, after)
    assert result.coverage[0] == 0
    assert result.coverage[-1] == 20
    assert result.offsets[before] == 0, "day zero moved"


def test_thin_offsets_are_not_reported_as_findings():
    """Two observations is not a result; it must not be averaged into one."""
    before = after = 2
    paths = {("S0", date(2024, 1, 1)): [0.5] * 5}
    events = [AttentionEvent("S0", date(2024, 1, 1), 3.0, 10)]
    result = stack_events(events, make_lookup(paths), before, after, min_dates=5)
    assert all(r == 0.0 for r in result.mean_return)


def test_unpriceable_symbol_is_skipped_not_zeroed():
    events = [AttentionEvent("BBBYQ", date(2024, 1, 1), 3.0, 10)]
    result = stack_events(events, make_lookup({}), 5, 5)
    assert result.n_events == 0
