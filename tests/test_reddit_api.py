"""Reddit API collector tests, with PRAW faked out."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.reddit_api import (
    MissingCredentials,
    RedditCollector,
    build_client,
    compare_sources,
)


def comment(cid, body="GME calls", author="someone", ts=1700000000, score=5):
    return SimpleNamespace(
        id=cid, body=body, score=score, created_utc=ts,
        author=SimpleNamespace(__str__=lambda self: author) if author else None,
        parent_id="t3_abc", is_submitter=False,
    )


class FakeComments:
    def __init__(self, items):
        self._items = items
        self.expanded_with = None

    def replace_more(self, limit=None):
        self.expanded_with = limit

    def list(self):
        return self._items


class FakeClient:
    def __init__(self, items):
        self.comments = FakeComments(items)

    def submission(self, id):
        return SimpleNamespace(comments=self.comments)


def test_missing_credentials_is_explicit(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    with pytest.raises(MissingCredentials, match="REDDIT_CLIENT_ID"):
        build_client()


def test_records_match_the_archive_schema():
    """One schema means downstream code never learns which source it got."""
    client = FakeClient([comment("c1")])
    got = RedditCollector(client).thread_comments("t1", "moves_tomorrow", "wallstreetbets")
    for key in ("id", "created_utc", "author", "body", "score", "link_id",
                "_thread_id", "_thread_type", "_subreddit"):
        assert key in got[0], f"missing {key} that the archive provides"
    assert got[0]["link_id"] == "t3_t1"
    assert got[0]["_source"] == "reddit_api"


def test_deleted_comments_are_counted_not_dropped():
    """Deletion is this source's bias; its size must be measurable."""
    client = FakeClient([comment("c1"), comment("c2", body="[deleted]"),
                         comment("c3", body="[removed]")])
    collector = RedditCollector(client)
    got = collector.thread_comments("t1", "moves_tomorrow", "wallstreetbets")

    assert len(got) == 3, "deleted comments were dropped instead of counted"
    assert collector.stats.deleted_bodies == 2
    assert collector.stats.as_dict()["deleted_share"] == pytest.approx(2 / 3)


def test_cap_truncates_and_is_recorded():
    client = FakeClient([comment(f"c{i}") for i in range(50)])
    collector = RedditCollector(client)
    got = collector.thread_comments("t1", "moves_tomorrow", "wsb", max_comments=10)
    assert len(got) == 10
    assert collector.stats.truncated_threads == 1


def test_uncapped_thread_is_not_flagged_truncated():
    client = FakeClient([comment(f"c{i}") for i in range(5)])
    collector = RedditCollector(client)
    collector.thread_comments("t1", "moves_tomorrow", "wsb", max_comments=10)
    assert collector.stats.truncated_threads == 0


def test_tree_expansion_is_bounded():
    """Unbounded expansion costs hundreds of requests on a large thread."""
    client = FakeClient([comment("c1")])
    RedditCollector(client, expand_batches=4).thread_comments("t1", "x", "wsb")
    assert client.comments.expanded_with == 4


def test_deleted_author_does_not_crash():
    c = comment("c1")
    c.author = None
    got = RedditCollector(FakeClient([c])).thread_comments("t1", "x", "wsb")
    assert got[0]["author"] == "[deleted]"


# --- source comparison -----------------------------------------------------

def test_compare_sources_quantifies_the_gap():
    """Before mixing sources, the difference should be a number, not a guess."""
    archive = [{"id": "a", "body": "real text"}, {"id": "b", "body": "also real"},
               {"id": "c", "body": "gone from api"}]
    api = [{"id": "a", "body": "real text"}, {"id": "b", "body": "[deleted]"},
           {"id": "d", "body": "new reply"}]

    report = compare_sources(archive, api)
    assert report["in_both"] == 2
    assert report["archive_only"] == 1
    assert report["api_only"] == 1
    assert report["text_lost_to_deletion"] == 1, \
        "text the archive preserved but the API has lost was not detected"


def test_compare_sources_on_identical_input():
    same = [{"id": "a", "body": "x"}]
    report = compare_sources(same, same)
    assert report["overlap_share"] == 1.0
    assert report["text_lost_to_deletion"] == 0
