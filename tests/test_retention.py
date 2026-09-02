"""Deletion-honouring tests.

Someone who deletes a comment has withdrawn it; a local copy that survives
defeats that. These tests pin the behaviour the API request commits us to.
"""

import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.retention import (
    RetentionReport,
    filter_withdrawn,
    is_withdrawn,
    purge_deleted,
)
from reddit_alpha.storage import RawStore


@pytest.mark.parametrize("body", ["[deleted]", "[removed]", "  [deleted]  "])
def test_withdrawn_bodies_are_recognised(body):
    assert is_withdrawn({"id": "a", "body": body})


def test_withdrawn_submissions_are_recognised():
    """Submissions carry their text in selftext, not body."""
    assert is_withdrawn({"id": "a", "selftext": "[removed]"})


def test_live_content_is_not_withdrawn():
    assert not is_withdrawn({"id": "a", "body": "GME to the moon"})
    assert not is_withdrawn({"id": "a", "body": ""})


def test_deleted_account_alone_does_not_remove_live_text():
    """A deleted account does not withdraw the comment.

    The text is still public on Reddit and the handle is only used to count
    distinct participants. Dropping these would discard live content and
    distort the counts -- 26% of authors in a 2023 sample were already deleted.
    """
    record = {"id": "a", "author": "[deleted]", "body": "I am buying calls tomorrow"}
    assert not is_withdrawn(record)
    assert filter_withdrawn([record]) == [record]


def test_filter_drops_withdrawn_and_counts_them():
    report = RetentionReport()
    kept = filter_withdrawn(
        [{"id": "a", "body": "real"}, {"id": "b", "body": "[deleted]"},
         {"id": "c", "body": "[removed]"}],
        report,
    )
    assert [r["id"] for r in kept] == ["a"]
    assert report.scanned == 3 and report.dropped_at_ingest == 2
    assert report.as_dict()["dropped_share"] == pytest.approx(2 / 3)


# --- purging an existing archive -------------------------------------------

def seed(tmp_path, records):
    store = RawStore(tmp_path / "raw")
    store.append("comments/x", records)
    return store


def test_purge_removes_withdrawn_records_from_disk(tmp_path):
    seed(tmp_path, [
        {"id": "a", "created_utc": 1704067200, "body": "real"},
        {"id": "b", "created_utc": 1704067300, "body": "[deleted]"},
    ])
    report = purge_deleted(tmp_path / "raw", "comments/x")
    left = list(RawStore(tmp_path / "raw").read("comments/x"))
    assert [r["id"] for r in left] == ["a"]
    assert report.dropped_at_ingest == 1
    assert report.files_rewritten == ["2024-01.jsonl.gz"]


def test_purge_removes_content_deleted_since_collection(tmp_path):
    """The harder case: it was live when collected and has since been deleted."""
    seed(tmp_path, [
        {"id": "a", "created_utc": 1704067200, "body": "still here"},
        {"id": "b", "created_utc": 1704067300, "body": "deleted since"},
    ])
    report = purge_deleted(tmp_path / "raw", "comments/x", still_present=lambda ids: {"a"})
    left = list(RawStore(tmp_path / "raw").read("comments/x"))
    assert [r["id"] for r in left] == ["a"]
    assert report.dropped_on_refresh == 1


def test_purge_leaves_a_clean_archive_untouched(tmp_path):
    """No rewrite when nothing changed, so timestamps stay meaningful."""
    seed(tmp_path, [{"id": "a", "created_utc": 1704067200, "body": "real"}])
    report = purge_deleted(tmp_path / "raw", "comments/x")
    assert report.files_rewritten == [] and report.dropped == 0


def test_purge_batches_the_liveness_check(tmp_path):
    """Checking ids one at a time would be thousands of needless requests."""
    seed(tmp_path, [
        {"id": f"r{i}", "created_utc": 1704067200 + i, "body": "text"} for i in range(250)
    ])
    calls = []

    def still_present(ids):
        calls.append(len(ids))
        return set(ids)

    purge_deleted(tmp_path / "raw", "comments/x", still_present, batch_size=100)
    assert calls == [100, 100, 50]


def test_purge_survives_an_interruption(tmp_path):
    """Rewrites go through a temp file, so a crash cannot truncate the archive."""
    seed(tmp_path, [{"id": "a", "created_utc": 1704067200, "body": "real"},
                    {"id": "b", "created_utc": 1704067300, "body": "[deleted]"}])
    path = tmp_path / "raw" / "comments/x" / "2024-01.jsonl.gz"
    original = path.read_bytes()
    try:
        purge_deleted(tmp_path / "raw", "comments/x",
                      still_present=lambda ids: (_ for _ in ()).throw(RuntimeError("boom")))
    except RuntimeError:
        pass
    assert path.read_bytes() == original, "archive was damaged by a failed purge"


def test_purge_on_missing_dataset_is_a_noop(tmp_path):
    assert purge_deleted(tmp_path / "raw", "nope").scanned == 0
