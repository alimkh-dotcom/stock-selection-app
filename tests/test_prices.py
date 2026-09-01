"""Price layer tests, focused on the ways this step can silently lie."""

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.prices import PriceSeries, PriceStore, _parse_chart


def series(prices, volumes=None, start=date(2024, 1, 1), first_trade=date(2020, 1, 1)):
    dates = [date.fromordinal(start.toordinal() + i) for i in range(len(prices))]
    return PriceSeries("TEST", dates, list(map(float, prices)),
                       [float(v) for v in (volumes or [1_000_000] * len(prices))],
                       first_trade)


# --- point-in-time gating --------------------------------------------------

def test_mentions_before_listing_are_rejected():
    """RDDT was matched in 2022 text; Reddit listed in 2024."""
    s = series([10] * 5, first_trade=date(2024, 3, 21))
    assert not s.was_listed_on(date(2022, 3, 1))
    assert s.was_listed_on(date(2024, 6, 1))


def test_unknown_listing_date_is_treated_as_not_listed():
    """Absent evidence, exclude -- including it would admit anachronisms."""
    assert not series([10] * 3, first_trade=None).was_listed_on(date(2024, 1, 2))


# --- returns ---------------------------------------------------------------

def test_forward_return_is_computed_forward_only():
    s = series([100, 110, 120, 130])
    assert s.forward_return(date(2024, 1, 1), 1) == pytest.approx(0.10)
    assert s.forward_return(date(2024, 1, 1), 2) == pytest.approx(0.20)


def test_forward_return_past_the_end_is_none_not_zero():
    """A truncated window must not be read as a flat return."""
    s = series([100, 110])
    assert s.forward_return(date(2024, 1, 1), 5) is None


def test_forward_return_on_a_missing_day_is_none():
    assert series([100, 110]).forward_return(date(2023, 6, 1), 1) is None


def test_window_returns_span_both_sides_of_the_event():
    """Whether chatter leads or follows price is the question, so the days
    before the event matter as much as the days after."""
    s = series([100, 110, 121, 133.1, 146.41])
    out = s.window_returns(date(2024, 1, 3), before=2, after=2)
    assert len(out) == 5
    assert out[0] is None          # needs a prior close, which day 0 lacks
    assert out[1] == pytest.approx(0.10)
    assert out[2] == pytest.approx(0.10)


def test_window_returns_pad_rather_than_shift_at_the_edges():
    """Padding keeps day zero aligned; shifting would corrupt every stack."""
    s = series([100, 110, 120])
    out = s.window_returns(date(2024, 1, 1), before=3, after=1)
    assert len(out) == 5
    assert out[:3] == [None, None, None]


# --- size bucketing --------------------------------------------------------

def test_dollar_volume_uses_only_past_days():
    """A stock's size bucket must not depend on what it did afterwards."""
    s = series([10, 10, 10, 1000], volumes=[1e6, 1e6, 1e6, 1e9])
    early = s.dollar_volume(date(2024, 1, 2), lookback=2)
    assert early == pytest.approx(1e7), "future volume leaked into a past bucket"


@pytest.mark.parametrize("price,volume,expected", [
    (100, 50_000_000, "mega"),     # $5bn/day
    (50, 10_000_000, "large"),     # $500m/day
    (10, 10_000_000, "mid"),       # $100m/day
    (5, 2_000_000, "small"),       # $10m/day
    (1, 100_000, "micro"),         # $100k/day
])
def test_size_buckets(price, volume, expected):
    s = series([price] * 5, volumes=[volume] * 5)
    assert s.size_bucket(date(2024, 1, 5), lookback=5) == expected


def test_newly_listed_flag():
    s = series([10] * 5, start=date(2024, 6, 1), first_trade=date(2024, 3, 1))
    assert s.is_newly_listed(date(2024, 6, 1))
    assert not s.is_newly_listed(date(2026, 6, 1))


def test_penny_flag():
    assert series([2.0] * 3).is_penny(date(2024, 1, 1)) is True
    assert series([50.0] * 3).is_penny(date(2024, 1, 1)) is False


# --- parsing ---------------------------------------------------------------

def _chart(ts, closes, adj=None, volumes=None, ftd=1577836800):
    return {
        "meta": {"firstTradeDate": ftd},
        "timestamp": ts,
        "indicators": {
            "quote": [{"close": closes, "volume": volumes or [1] * len(ts)}],
            **({"adjclose": [{"adjclose": adj}]} if adj else {}),
        },
    }


def test_adjusted_close_is_preferred_over_raw_close():
    """Raw close would turn a stock split into a 50% crash."""
    payload = _chart([1704067200, 1704153600], [200, 100], adj=[100, 100])
    parsed = _parse_chart("X", payload)
    assert parsed.adj_close == [100.0, 100.0]


def test_null_bars_are_skipped_not_zeroed():
    """A null close treated as zero would fabricate a -100% return."""
    payload = _chart([1704067200, 1704153600, 1704240000], [100, None, 110])
    parsed = _parse_chart("X", payload)
    assert parsed.adj_close == [100.0, 110.0]
    assert len(parsed.dates) == 2


def test_first_trade_date_is_parsed():
    parsed = _parse_chart("X", _chart([1704067200], [100], ftd=1577836800))
    assert parsed.first_trade_date == date(2020, 1, 1)


def test_empty_chart_yields_nothing():
    assert _parse_chart("X", {"timestamp": [], "indicators": {}}) is None


# --- survivorship reporting ------------------------------------------------

def test_unpriceable_symbols_are_counted_not_dropped(tmp_path, monkeypatch):
    """Delisted names are the ones Reddit hyped; losing them flatters results."""
    store = PriceStore(tmp_path)
    monkeypatch.setattr(store, "_raw", lambda *a, **k: (None, "no_data"))
    assert store.get("BBBYQ", date(2022, 1, 1), date(2023, 1, 1)) is None
    assert "BBBYQ" in store.report.unpriceable
    assert store.report.as_dict()["unpriceable_share"] == 1.0


def test_transient_failure_is_not_recorded_as_a_delisting(tmp_path, monkeypatch):
    """SOFI failed once on a bad response and was nearly written off as dead.

    Counting a network hiccup as a delisting corrupts the survivorship number,
    which is a headline result of the whole study.
    """
    store = PriceStore(tmp_path)
    monkeypatch.setattr(store, "_raw", lambda *a, **k: (None, "error"))
    assert store.get("SOFI", date(2022, 1, 1), date(2023, 1, 1)) is None
    assert "SOFI" not in store.report.unpriceable
    assert "SOFI" in store.report.fetch_failed


def test_transient_failure_leaves_no_poisoned_cache(tmp_path):
    """A cached 'null' from a hiccup would make the symbol permanently dead."""
    store = PriceStore(tmp_path, max_retries=1)
    class Boom:
        def get(self, *a, **k):
            raise __import__("requests").RequestException("network down")
    store.session = Boom()
    store.get("SOFI", date(2022, 1, 1), date(2023, 1, 1))
    assert not (tmp_path / "SOFI.json").exists(), "cached a guess as fact"


def test_negative_results_are_cached(tmp_path):
    """Thousands of delisted symbols must not be re-requested every run."""
    store = PriceStore(tmp_path)
    (tmp_path / "GONE.json").write_text("null")
    calls = []
    store.session.get = lambda *a, **k: calls.append(1)  # type: ignore[assignment]
    assert store.get("GONE", date(2022, 1, 1), date(2023, 1, 1)) is None
    assert not calls, "re-requested a symbol already known to be unavailable"
