"""Tests for thread classification and resumability."""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.arctic import ArcticShiftError
from reddit_alpha.collect import Collector, classify_thread, day_range
from reddit_alpha.storage import Manifest, RawStore


# Real titles observed in the archive, one per era. The naming and the posting
# account both changed over the years, so these are regression cases: if a
# future tweak stops matching any of them, a whole year of Track A goes missing.
@pytest.mark.parametrize(
    "title,expected",
    [
        ("[Discussion] What Are Your Moves Tomorrow, March 02", "moves_tomorrow"),
        ("What Are Your Moves Tomorrow, March 08, 2024", "moves_tomorrow"),
        ("Daily Discussion Thread - March 06 2019", "daily_discussion"),
        ("Unpinned Daily Discussion Thread for March 04 2021", "daily_discussion"),
        ("Daily Discussion Thread for March 06, 2023", "daily_discussion"),
        ("Weekend Discussion Thread for the Weekend of March 07 2025", "weekend_discussion"),
        ("Weekly Tendies Thread - March 08 2019", "weekly_tendies"),
        ("GME to the moon 🚀🚀🚀", None),
        # A user post that merely mentions the phrase is not the official
        # thread; matching it would pull ordinary posts into Track A.
        ("My moves tomorrow are none of your business", None),
        ("What are your moves tomorrow?", "moves_tomorrow"),
    ],
)
def test_classify_thread(title, expected):
    assert classify_thread(title) == expected


def test_day_range_is_half_open():
    days = list(day_range(date(2024, 1, 1), date(2024, 1, 4)))
    assert days == [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]


class StubClient:
    """Returns canned posts; can be told to fail for specific days."""

    def __init__(self, per_day=2, fail_on=()):
        self.per_day = per_day
        self.fail_on = set(fail_on)
        self.requested = []

    def search_posts(self, **params):
        self.requested.append(params["after"])
        if params["after"] in self.fail_on:
            raise ArcticShiftError("simulated outage")
        base = int(date.fromisoformat(params["after"]).strftime("%s"))
        return iter(
            [
                {"id": f"{params['after']}-{i}", "created_utc": base + i, "title": "x"}
                for i in range(self.per_day)
            ]
        )

    def thread_comments(self, post_id):
        return iter([{"id": f"{post_id}-c{i}", "created_utc": 1700000000 + i} for i in range(3)])


def build(tmp_path, client):
    return Collector(client, RawStore(tmp_path / "raw"), Manifest(tmp_path / "m.db"))


def test_crawl_writes_and_counts(tmp_path):
    c = build(tmp_path, StubClient())
    stats = c.crawl_posts(["stocks"], date(2024, 1, 1), date(2024, 1, 4))
    assert stats.units_done == 3
    assert stats.records == 6


def test_completed_days_are_not_refetched(tmp_path):
    """Resumability: a second run must do no network work for finished days."""
    client = StubClient()
    c = build(tmp_path, client)
    c.crawl_posts(["stocks"], date(2024, 1, 1), date(2024, 1, 4))
    first_pass = len(client.requested)

    c2 = Collector(client, RawStore(tmp_path / "raw"), Manifest(tmp_path / "m.db"))
    stats = c2.crawl_posts(["stocks"], date(2024, 1, 1), date(2024, 1, 4))

    assert stats.units_done == 0, "re-crawled days that were already complete"
    assert len(client.requested) == first_pass, "made network calls for finished days"


def test_failed_day_is_recorded_and_crawl_continues(tmp_path):
    """A single bad day must not abort a multi-hour crawl, nor vanish silently."""
    client = StubClient(fail_on={"2024-01-02"})
    c = build(tmp_path, client)
    stats = c.crawl_posts(["stocks"], date(2024, 1, 1), date(2024, 1, 4))

    assert stats.units_done == 2
    assert stats.units_failed == 1
    issues = c.manifest.issues()
    assert len(issues) == 1 and "2024-01-02" in issues[0][0]


def test_failed_day_is_retried_on_resume(tmp_path):
    """A day that failed was never marked done, so a later run must retry it."""
    c = build(tmp_path, StubClient(fail_on={"2024-01-02"}))
    c.crawl_posts(["stocks"], date(2024, 1, 1), date(2024, 1, 4))

    recovered = StubClient()
    c2 = Collector(recovered, RawStore(tmp_path / "raw"), Manifest(tmp_path / "m.db"))
    stats = c2.crawl_posts(["stocks"], date(2024, 1, 1), date(2024, 1, 4))

    assert stats.units_done == 1
    assert recovered.requested == ["2024-01-02"]


def test_daily_threads_found_from_stored_posts(tmp_path):
    """Stage 2 reads what stage 1 stored -- it must make no network calls."""
    store = RawStore(tmp_path / "raw")
    store.append(
        "posts/wallstreetbets",
        [
            {"id": "a", "created_utc": 1700000000, "title": "What Are Your Moves Tomorrow, May 1"},
            {"id": "b", "created_utc": 1700000100, "title": "YOLO update"},
            {"id": "c", "created_utc": 1700000200, "title": "Daily Discussion Thread for May 1"},
        ],
    )
    c = Collector(None, store, Manifest(tmp_path / "m.db"))  # None proves no network use
    threads = c.find_daily_threads()

    assert [t["id"] for t in threads] == ["a", "c"]
    assert threads[0]["thread_type"] == "moves_tomorrow"


def test_comments_tagged_with_thread_type(tmp_path):
    c = build(tmp_path, StubClient())
    threads = [
        {"id": "t1", "created_utc": 1700000000, "thread_type": "moves_tomorrow",
         "subreddit": "wallstreetbets", "title": "x", "author": "y"}
    ]
    c.crawl_thread_comments(threads)
    stored = list(c.store.read("comments/moves_tomorrow"))
    assert len(stored) == 3
    assert all(r["_thread_type"] == "moves_tomorrow" for r in stored)
    assert all(r["_thread_id"] == "t1" for r in stored)
