"""Kaggle loader tests.

The failure modes here are silent ones: a mis-mapped column yields a corpus of
empty strings that still looks like data, and milliseconds read as seconds put
every comment in the year 56000 where it matches no trading day.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.kaggle_source import (
    SchemaError,
    ingest_csv,
    normalise_row,
    parse_timestamp,
    resolve_columns,
)
from reddit_alpha.storage import RawStore


# --- column mapping --------------------------------------------------------

def test_maps_canonical_names():
    m = resolve_columns(["id", "created_utc", "body", "author", "score"])
    assert m["body"] == "body" and m["created_utc"] == "created_utc"


@pytest.mark.parametrize("header,expected_body", [
    (["comment_id", "timestamp", "comment"], "comment"),
    (["id", "created", "text"], "text"),
    (["post_id", "date", "selftext"], "selftext"),
])
def test_maps_the_aliases_real_dumps_use(header, expected_body):
    """Every uploader names columns differently; guessing wrong is silent."""
    assert resolve_columns(header)["body"] == expected_body


def test_mapping_is_case_insensitive():
    assert resolve_columns(["ID", "Created_UTC", "Body"])["body"] == "Body"


def test_unmappable_header_raises_rather_than_guessing():
    """A corpus of empty bodies is worse than a failed load."""
    with pytest.raises(SchemaError, match="created_utc"):
        resolve_columns(["foo", "bar", "body"])


def test_error_shows_the_actual_header():
    with pytest.raises(SchemaError, match="wibble"):
        resolve_columns(["wibble", "wobble"])


# --- timestamps ------------------------------------------------------------

def test_unix_seconds():
    assert parse_timestamp(1704067200) == 1704067200
    assert parse_timestamp("1704067200") == 1704067200


def test_milliseconds_are_detected_not_trusted():
    """The classic bug: ms read as seconds lands records in the year 56000,
    where they silently match no trading day at all."""
    assert parse_timestamp(1704067200000) == 1704067200


@pytest.mark.parametrize("text", [
    "2024-01-01 00:00:00", "2024-01-01T00:00:00", "2024-01-01",
    "2024-01-01T00:00:00Z",
])
def test_iso_dialects(text):
    assert parse_timestamp(text) == 1704067200


@pytest.mark.parametrize("bad", [
    None, "", "not a date", 0, -1,
    946684800,        # 2000 -- predates Reddit
    99999999999999,   # far future even after ms correction
])
def test_implausible_timestamps_are_rejected(bad):
    assert parse_timestamp(bad) is None


# --- row normalisation -----------------------------------------------------

def _map():
    return resolve_columns(["id", "created_utc", "body", "author", "score", "subreddit"])


def test_normalises_a_good_row():
    row = {"id": "abc", "created_utc": "1704067200", "body": "GME calls",
           "author": "someone", "score": "42", "subreddit": "wallstreetbets"}
    rec = normalise_row(row, _map(), "src", 0)
    assert rec["id"] == "abc" and rec["created_utc"] == 1704067200
    assert rec["score"] == 42 and rec["_source"] == "src"


def test_every_record_is_tagged_with_its_dataset():
    """Datasets must not be stitched; the tag is what makes that enforceable."""
    rec = normalise_row({"created_utc": "1704067200", "body": "x", "id": "a"},
                        _map(), "mattpodolak/wsb", 0)
    assert rec["_source"] == "mattpodolak/wsb"


def test_rows_without_usable_time_or_text_are_dropped():
    m = _map()
    assert normalise_row({"id": "a", "created_utc": "junk", "body": "x"}, m, "s", 0) is None
    assert normalise_row({"id": "a", "created_utc": "1704067200", "body": "  "}, m, "s", 0) is None


def test_non_numeric_score_is_dropped_not_fatal():
    rec = normalise_row({"id": "a", "created_utc": "1704067200", "body": "x",
                         "score": "N/A"}, _map(), "s", 0)
    assert "score" not in rec


def test_missing_id_gets_a_stable_synthetic_one():
    m = resolve_columns(["created_utc", "body"])
    rec = normalise_row({"created_utc": "1704067200", "body": "x"}, m, "src", 7)
    assert rec["id"] == "src:7"


# --- end to end ------------------------------------------------------------

def write_csv(tmp_path, text):
    p = tmp_path / "in.csv"
    p.write_text(text, encoding="utf-8")
    return p


def test_ingest_writes_records_under_a_per_source_path(tmp_path):
    csv_path = write_csv(tmp_path, (
        "id,created_utc,body,author\n"
        "a,1704067200,GME to the moon,alice\n"
        "b,1704153600,NVDA calls,bob\n"
    ))
    store = RawStore(tmp_path / "raw")
    report = ingest_csv(csv_path, store, "test/ds", batch_size=1)

    assert report.rows_written == 2
    stored = list(store.read("kaggle/test/ds"))
    assert {r["body"] for r in stored} == {"GME to the moon", "NVDA calls"}
    assert all(r["_source"] == "test/ds" for r in stored)


def test_ingest_drops_withdrawn_content(tmp_path):
    csv_path = write_csv(tmp_path, (
        "id,created_utc,body\n"
        "a,1704067200,real comment\n"
        "b,1704153600,[deleted]\n"
        "c,1704240000,[removed]\n"
    ))
    store = RawStore(tmp_path / "raw")
    report = ingest_csv(csv_path, store, "test/ds")
    assert report.rows_written == 1 and report.withdrawn == 2


def test_ingest_counts_what_it_discards(tmp_path):
    """A load that silently drops half the file should be visible, not guessed at."""
    csv_path = write_csv(tmp_path, (
        "id,created_utc,body\n"
        "a,1704067200,fine\n"
        "b,junk,also fine\n"
        "c,1704240000,\n"
        "d,1704240000,fine too\n"
    ))
    report = ingest_csv(write_csv(tmp_path, csv_path.read_text()),
                        RawStore(tmp_path / "raw"), "test/ds")
    assert report.rows_read == 4
    assert report.bad_timestamps == 1 and report.missing_text == 1
    assert report.as_dict()["kept_share"] == pytest.approx(0.5)


def test_ingest_deduplicates_within_a_file(tmp_path):
    csv_path = write_csv(tmp_path, (
        "id,created_utc,body\n"
        "a,1704067200,one\n"
        "a,1704067200,one\n"
    ))
    report = ingest_csv(csv_path, RawStore(tmp_path / "raw"), "test/ds")
    assert report.rows_written == 1 and report.duplicates == 1


def test_ingest_batches_do_not_lose_the_remainder(tmp_path):
    """An off-by-one here would silently truncate every load."""
    rows = "".join(f"r{i},{1704067200 + i},body {i}\n" for i in range(25))
    csv_path = write_csv(tmp_path, "id,created_utc,body\n" + rows)
    report = ingest_csv(csv_path, RawStore(tmp_path / "raw"), "test/ds", batch_size=10)
    assert report.rows_written == 25


def test_ingest_survives_bad_encoding(tmp_path):
    p = tmp_path / "in.csv"
    p.write_bytes(b"id,created_utc,body\na,1704067200,caf\xe9 GME\n")
    report = ingest_csv(p, RawStore(tmp_path / "raw"), "test/ds")
    assert report.rows_written == 1
