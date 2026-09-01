import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.storage import Manifest, RawStore, month_of


def test_records_partition_by_creation_month(tmp_path):
    store = RawStore(tmp_path)
    store.append("posts/x", [
        {"id": "a", "created_utc": 1704067200},  # 2024-01-01
        {"id": "b", "created_utc": 1706745600},  # 2024-02-01
    ])
    assert store.months("posts/x") == ["2024-01", "2024-02"]


def test_appends_do_not_overwrite(tmp_path):
    """The raw archive is append-only; a second crawl must not truncate it."""
    store = RawStore(tmp_path)
    store.append("posts/x", [{"id": "a", "created_utc": 1704067200}])
    store.append("posts/x", [{"id": "b", "created_utc": 1704067300}])
    assert {r["id"] for r in store.read("posts/x")} == {"a", "b"}


def test_unicode_survives_roundtrip(tmp_path):
    """Reddit is full of emoji; mangling them would corrupt sentiment scoring."""
    store = RawStore(tmp_path)
    store.append("c", [{"id": "a", "created_utc": 1704067200, "body": "GME 🚀🚀 café"}])
    assert next(iter(store.read("c")))["body"] == "GME 🚀🚀 café"


def test_read_missing_dataset_is_empty(tmp_path):
    assert list(RawStore(tmp_path).read("nope")) == []


def test_manifest_tracks_completion(tmp_path):
    m = Manifest(tmp_path / "m.db")
    assert not m.is_done("u1")
    m.mark_done("u1", "posts", 10)
    assert m.is_done("u1")
    assert m.summary()["posts"] == {"units": 1, "records": 10}


def test_manifest_persists_across_instances(tmp_path):
    """Resumability depends on this surviving a process restart."""
    Manifest(tmp_path / "m.db").mark_done("u1", "posts", 5)
    assert Manifest(tmp_path / "m.db").is_done("u1")


def test_issues_are_retained(tmp_path):
    m = Manifest(tmp_path / "m.db")
    m.note_issue("u1", "gap", "lost records")
    assert m.issues()[0][:3] == ("u1", "gap", "lost records")


def test_month_of_uses_utc():
    # 2024-01-01 00:30 UTC -- must not drift into December under a local timezone.
    assert month_of(1704069000) == "2024-01"
