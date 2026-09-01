"""Tests for pagination, which is where silent data loss would come from."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.arctic import MAX_PAGE, ArcticShiftClient


class FakeClient(ArcticShiftClient):
    """Serves records from a fixed list, mimicking the real API's semantics.

    Crucially it reproduces `after` being *exclusive* on created_utc, which is
    the behaviour the overlap logic exists to defend against.
    """

    def __init__(self, records):
        self.records = sorted(records, key=lambda r: r["created_utc"])
        self.calls = []

    def _request(self, path, params):
        self.calls.append(params)
        rows = self.records
        if params.get("after") is not None:
            rows = [r for r in rows if r["created_utc"] > int(params["after"])]
        if params.get("before") is not None:
            rows = [r for r in rows if r["created_utc"] < int(params["before"])]
        if params.get("sort") == "desc":
            rows = list(reversed(rows))
        return rows[: params.get("limit", MAX_PAGE)]


def rec(i, ts):
    return {"id": f"r{i}", "created_utc": ts}


def test_paginates_across_multiple_pages():
    records = [rec(i, 1000 + i) for i in range(250)]
    client = FakeClient(records)
    got = list(client.paginate("posts/search", {}))
    assert [r["id"] for r in got] == [r["id"] for r in records]


def test_no_records_lost_when_page_boundary_splits_a_second():
    """The exclusive-`after` trap.

    120 records share one timestamp. A page ends mid-second; naive paging with
    `after=last_ts` would skip the rest of that second outright.
    """
    records = [rec(i, 5000) for i in range(120)] + [rec(200 + i, 5001 + i) for i in range(30)]
    client = FakeClient(records)
    got = list(client.paginate("posts/search", {}))
    assert len(got) == len(records), "records were dropped at the page boundary"
    assert {r["id"] for r in got} == {r["id"] for r in records}


def test_deduplicates_the_overlap():
    """The one-second overlap re-reads records; they must be emitted once."""
    records = [rec(i, 1000 + (i // 10)) for i in range(150)]
    client = FakeClient(records)
    got = list(client.paginate("posts/search", {}))
    ids = [r["id"] for r in got]
    assert len(ids) == len(set(ids)), "overlap produced duplicate records"


def test_crowded_second_recovered_by_reading_backwards(caplog):
    """A second holding more than one page is still fully recoverable.

    Ascending order reaches the first 100; descending reaches the last 100.
    For a second holding 150, the two ends overlap and nothing is lost.
    """
    records = [rec(i, 7000) for i in range(150)] + [rec(999, 8000)]
    client = FakeClient(records)
    with caplog.at_level("WARNING"):
        got = list(client.paginate("posts/search", {}))
    assert len(got) == len(records), "records lost in a recoverable crowded second"
    assert not any("ARE LOST" in r.message for r in caplog.records), \
        "reported loss despite full recovery"
    assert any(r["id"] == "r999" for r in got), "crawl did not move past the stall"


def test_truly_unreachable_records_are_reported(caplog):
    """Beyond 2 pages in one second the API offers no cursor to the middle.

    That gap is real. It must be reported, not hidden, and the crawl must
    still move on.
    """
    records = [rec(i, 7000) for i in range(2 * MAX_PAGE + 40)] + [rec(999, 8000)]
    client = FakeClient(records)
    with caplog.at_level("ERROR"):
        got = list(client.paginate("posts/search", {}))
    assert any("RECORDS ARE LOST" in r.message for r in caplog.records), \
        "unreachable records were not reported"
    assert any(r["id"] == "r999" for r in got), "crawl did not move past the stall"


def test_stops_on_short_page():
    client = FakeClient([rec(i, 1000 + i) for i in range(10)])
    list(client.paginate("posts/search", {}))
    assert len(client.calls) == 1, "kept requesting after an obviously final page"


def test_respects_stop_after():
    client = FakeClient([rec(i, 1000 + i) for i in range(250)])
    assert len(list(client.paginate("posts/search", {}, stop_after=15))) == 15
