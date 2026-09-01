"""Daily prices, and the point-in-time discipline around them.

Three hazards this module exists to handle:

**Look-ahead through adjustment.** Returns use adjusted close so that splits and
dividends do not masquerade as price moves.

**Companies that had not yet listed.** The symbol list describes today. Matching
it against old text produces anachronisms -- ``RDDT`` was extracted from March
2022 comments, two years before Reddit went public. Every symbol carries its
first trade date and mentions before that date are discarded.

**Companies that no longer exist.** Delisted names are only partly retrievable,
and they are exactly the ones Reddit hyped: ``FTCHQ`` returns data, ``SIVB`` and
``BBBYQ`` return nothing. Silently dropping them would delete the worst outcomes
from the study and flatter every result. They are counted, reported, and treated
as total losses rather than omitted.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Average daily dollar volume, in dollars. Market cap is unavailable from this
# source, and using today's market cap to bucket old data would sort companies
# by how big they eventually became -- the same hindsight trap the project is
# built to avoid. Dollar volume is computable correctly at every past date and
# also says how much money could realistically be deployed.
SIZE_BUCKETS = [
    ("mega", 1_000_000_000),
    ("large", 200_000_000),
    ("mid", 50_000_000),
    ("small", 5_000_000),
    ("micro", 0),
]

NEWLY_LISTED_DAYS = 365
PENNY_PRICE = 5.0


@dataclass
class PriceSeries:
    """One symbol's daily history, oldest first."""

    symbol: str
    dates: list[date]
    adj_close: list[float]
    volume: list[float]
    first_trade_date: date | None

    def __len__(self) -> int:
        return len(self.dates)

    def _index_map(self) -> dict[date, int]:
        if not hasattr(self, "_idx"):
            object.__setattr__(self, "_idx", {d: i for i, d in enumerate(self.dates)})
        return self._idx  # type: ignore[attr-defined]

    def was_listed_on(self, day: date) -> bool:
        """Whether the company was publicly traded on ``day``.

        Guards against anachronistic mentions: a symbol reused by a company that
        listed later must not be credited with chatter from before it existed.
        """
        if self.first_trade_date is None:
            return False
        return day >= self.first_trade_date

    def forward_return(self, day: date, horizon: int) -> float | None:
        """Return from the close of ``day`` to ``horizon`` trading days later.

        Returns None when the window runs past the available history, so a
        truncated window is never silently treated as a flat return.
        """
        idx = self._index_map().get(day)
        if idx is None or idx + horizon >= len(self.dates):
            return None
        start, end = self.adj_close[idx], self.adj_close[idx + horizon]
        if not start or start <= 0:
            return None
        return end / start - 1.0

    def window_returns(self, day: date, before: int, after: int) -> list[float | None]:
        """Daily returns from ``-before`` to ``+after`` trading days around ``day``.

        This is what event stacking consumes: it needs the days *before* the
        event as much as the days after, since whether chatter leads or follows
        price is the entire question.
        """
        idx = self._index_map().get(day)
        if idx is None:
            return [None] * (before + after + 1)
        out: list[float | None] = []
        for offset in range(-before, after + 1):
            i = idx + offset
            if i <= 0 or i >= len(self.dates):
                out.append(None)
                continue
            prev, cur = self.adj_close[i - 1], self.adj_close[i]
            out.append(cur / prev - 1.0 if prev and prev > 0 else None)
        return out

    def dollar_volume(self, day: date, lookback: int = 21) -> float | None:
        """Average daily dollar volume over the ``lookback`` days ending at ``day``.

        Uses only days at or before ``day`` -- the size bucket a stock sits in
        must not depend on what it did afterwards.
        """
        idx = self._index_map().get(day)
        if idx is None:
            return None
        lo = max(0, idx - lookback + 1)
        values = [
            self.adj_close[i] * self.volume[i]
            for i in range(lo, idx + 1)
            if self.adj_close[i] and self.volume[i]
        ]
        return sum(values) / len(values) if values else None

    def size_bucket(self, day: date, lookback: int = 21) -> str | None:
        dv = self.dollar_volume(day, lookback)
        if dv is None:
            return None
        for name, floor in SIZE_BUCKETS:
            if dv >= floor:
                return name
        return "micro"

    def is_newly_listed(self, day: date) -> bool:
        if self.first_trade_date is None:
            return False
        return 0 <= (day - self.first_trade_date).days <= NEWLY_LISTED_DAYS

    def is_penny(self, day: date) -> bool | None:
        idx = self._index_map().get(day)
        if idx is None:
            return None
        return self.adj_close[idx] < PENNY_PRICE


@dataclass
class PriceFetchReport:
    """What could and could not be priced.

    Kept deliberately prominent. The unpriceable symbols are disproportionately
    the delisted disasters, so their count is a headline result, not a footnote.

    ``unpriceable`` and ``fetch_failed`` are kept apart on purpose. The first
    means the provider has no data for this symbol -- a real delisting, and a
    real hole in the study. The second means the request itself went wrong, and
    is fixed by asking again. SOFI landed in the wrong bucket during an
    integration run and would have been silently written off as a dead company,
    which is exactly the kind of error that inflates a backtest.
    """

    fetched: set[str] = field(default_factory=set)
    unpriceable: set[str] = field(default_factory=set)
    fetch_failed: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        total = len(self.fetched) + len(self.unpriceable)
        return {
            "fetched": len(self.fetched),
            "unpriceable": len(self.unpriceable),
            "unpriceable_share": len(self.unpriceable) / total if total else 0.0,
            "unpriceable_symbols": sorted(self.unpriceable),
            "fetch_failed": len(self.fetch_failed),
            "fetch_failed_symbols": sorted(self.fetch_failed),
        }


class PriceStore:
    """Fetches and caches daily price history."""

    def __init__(
        self, cache_dir: Path, min_interval: float = 0.4, max_retries: int = 3
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.report = PriceFetchReport()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0 (reddit-alpha-research)"
        self._last_call = 0.0

    def _cache_path(self, symbol: str) -> Path:
        # Symbols contain '.' and '-'; keep them but avoid path separators.
        safe = symbol.replace("/", "_")
        return self.cache_dir / f"{safe}.json"

    def get(self, symbol: str, start: date, end: date, refresh: bool = False) -> PriceSeries | None:
        payload, outcome = self._raw(symbol, start, end, refresh)
        if payload is None:
            if outcome == "no_data":
                self.report.unpriceable.add(symbol)
            else:
                self.report.fetch_failed.add(symbol)
            return None
        series = _parse_chart(symbol, payload)
        if series is None or not len(series):
            self.report.unpriceable.add(symbol)
            return None
        self.report.fetched.add(symbol)
        return series

    def _raw(
        self, symbol: str, start: date, end: date, refresh: bool
    ) -> tuple[dict[str, Any] | None, str]:
        """Return ``(payload, outcome)`` where outcome is ok | no_data | error."""
        path = self._cache_path(symbol)
        if path.exists() and not refresh:
            try:
                # A cached ``null`` is a remembered failure, not a cache miss:
                # delisted symbols never become available, and re-asking for
                # thousands of them would dominate the run. Only an unreadable
                # file falls through to a fetch.
                cached = json.loads(path.read_text())
                return (cached, "ok") if cached else (None, "no_data")
            except json.JSONDecodeError:
                log.warning("Corrupt price cache for %s; refetching", symbol)

        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

        params = {
            "period1": int(datetime.combine(start, datetime.min.time()).timestamp()),
            "period2": int(datetime.combine(end, datetime.min.time()).timestamp()),
            "interval": "1d",
            "events": "div,split",
        }
        # A transient failure must never be cached as "no such company", so the
        # request is retried before any conclusion is drawn about the symbol.
        body: dict[str, Any] | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(
                    CHART_URL.format(symbol=symbol), params=params, timeout=45
                )
                body = resp.json()
                break
            except (requests.RequestException, ValueError) as exc:
                log.warning(
                    "Price fetch attempt %d failed for %s: %s", attempt + 1, symbol, exc
                )
                time.sleep(min(2**attempt, 15))
                self._last_call = time.monotonic()

        if body is None:
            # Still unknown after retrying. Leave the cache untouched so a later
            # run can settle it rather than inheriting a guess.
            return None, "error"

        chart = body.get("chart") or {}
        result = chart.get("result")
        if not result:
            # The provider answered and has nothing: a genuine delisting or an
            # unknown symbol. Worth caching -- thousands of these would otherwise
            # be re-requested every run.
            path.write_text("null")
            return None, "no_data"
        path.write_text(json.dumps(result[0]))
        return result[0], "ok"


def _parse_chart(symbol: str, payload: dict[str, Any]) -> PriceSeries | None:
    timestamps = payload.get("timestamp")
    if not timestamps:
        return None
    indicators = payload.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0]
    adj = (indicators.get("adjclose") or [{}])[0].get("adjclose")
    closes = adj if adj else quote.get("close")
    volumes = quote.get("volume")
    if not closes:
        return None

    meta = payload.get("meta") or {}
    ftd_raw = meta.get("firstTradeDate")
    first_trade = (
        datetime.fromtimestamp(ftd_raw, timezone.utc).date() if ftd_raw else None
    )

    dates: list[date] = []
    prices: list[float] = []
    vols: list[float] = []
    for i, ts in enumerate(timestamps):
        price = closes[i] if i < len(closes) else None
        if price is None:
            continue  # market holiday artefacts and gaps
        dates.append(datetime.fromtimestamp(ts, timezone.utc).date())
        prices.append(float(price))
        vol = volumes[i] if volumes and i < len(volumes) else None
        vols.append(float(vol) if vol else 0.0)

    if not dates:
        return None
    return PriceSeries(symbol, dates, prices, vols, first_trade)
