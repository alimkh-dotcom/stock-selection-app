"""Loading bulk Reddit corpora published on Kaggle.

These datasets are far larger than anything we could crawl, but they were each
assembled by a different person with different tools, so the risks are different
from a live API's. Three of them shape this module.

**Columns are named whatever the uploader felt like.** One dataset's ``body`` is
another's ``comment`` or ``text``; ``created_utc`` may be ``timestamp``,
``created`` or ``date``. Guessing wrong silently produces a corpus of empty
strings that still looks like data. Mapping is therefore explicit, and a column
that cannot be resolved raises with the actual header rather than defaulting.

**Timestamps lie in several dialects.** Unix seconds, unix milliseconds, and ISO
strings all appear. Reading milliseconds as seconds is the classic version of
this bug and puts every comment tens of thousands of years in the future, where
it quietly matches no trading day at all. Every parsed timestamp is therefore
range-checked against Reddit's own lifetime.

**They must not be stitched together.** Different collection methods and
completeness mean a jump in mention volume at the boundary between two datasets
is an artefact, not an event — and event studies are exactly what such artefacts
corrupt. Every record carries the dataset it came from, and records are stored
under a per-source path so an analysis has to opt in to combining them.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .retention import filter_withdrawn
from .storage import RawStore

log = logging.getLogger(__name__)

# Reddit launched in 2005; nothing legitimate predates it. The upper bound
# catches milliseconds-read-as-seconds, which lands records in the year 56000.
EARLIEST_PLAUSIBLE = int(datetime(2005, 1, 1, tzinfo=timezone.utc).timestamp())
LATEST_PLAUSIBLE = int(datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp())

# Column aliases seen across published Reddit dumps, most specific first.
ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "comment_id", "post_id", "name", "fullname"),
    "created_utc": ("created_utc", "created", "timestamp", "date", "datetime", "time"),
    "body": ("body", "comment", "text", "comment_body", "selftext", "title"),
    "author": ("author", "user", "username", "author_name"),
    "score": ("score", "ups", "upvotes", "num_upvotes"),
    "subreddit": ("subreddit", "sub", "subreddit_name"),
    "link_id": ("link_id", "parent_post_id", "submission_id", "post_id"),
}

REQUIRED = ("created_utc", "body")


class SchemaError(RuntimeError):
    """Raised when a file's columns cannot be mapped to the canonical schema."""


class MissingCredentials(RuntimeError):
    """Raised when Kaggle API credentials are absent from the environment."""


@dataclass
class IngestReport:
    source: str = ""
    rows_read: int = 0
    rows_written: int = 0
    bad_timestamps: int = 0
    missing_text: int = 0
    withdrawn: int = 0
    duplicates: int = 0
    column_mapping: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "kept_share": self.rows_written / self.rows_read if self.rows_read else 0.0,
            "bad_timestamps": self.bad_timestamps,
            "missing_text": self.missing_text,
            "withdrawn": self.withdrawn,
            "duplicates": self.duplicates,
            "column_mapping": self.column_mapping,
        }


def resolve_columns(header: Iterable[str]) -> dict[str, str]:
    """Map canonical field names onto this file's actual column names.

    Raises rather than guessing when a required field is absent: a corpus of
    empty bodies is worse than a failed load, because it still looks like data.
    """
    available = {h.strip().lower(): h for h in header}
    mapping: dict[str, str] = {}
    for canonical, candidates in ALIASES.items():
        for candidate in candidates:
            if candidate in available:
                mapping[canonical] = available[candidate]
                break

    missing = [f for f in REQUIRED if f not in mapping]
    if missing:
        raise SchemaError(
            f"Could not find column(s) {missing} in header {sorted(available.values())}. "
            "Add the actual name to ALIASES rather than letting the load proceed."
        )
    return mapping


def parse_timestamp(raw: Any) -> int | None:
    """Return a unix timestamp in seconds, or None if the value is unusable.

    Accepts unix seconds, unix milliseconds and ISO-8601 strings. Anything
    landing outside Reddit's lifetime is rejected rather than trusted.
    """
    if raw is None or raw == "":
        return None

    text = str(raw).strip()
    try:
        value = float(text)
        seconds = int(value / 1000) if value > LATEST_PLAUSIBLE else int(value)
        return seconds if EARLIEST_PLAUSIBLE <= seconds <= LATEST_PLAUSIBLE else None
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc)
            seconds = int(dt.timestamp())
            return seconds if EARLIEST_PLAUSIBLE <= seconds <= LATEST_PLAUSIBLE else None
        except ValueError:
            continue

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        seconds = int(dt.timestamp())
        return seconds if EARLIEST_PLAUSIBLE <= seconds <= LATEST_PLAUSIBLE else None
    except ValueError:
        return None


def normalise_row(
    row: dict[str, str], mapping: dict[str, str], source: str, row_number: int
) -> dict[str, Any] | None:
    """Convert one source row into the canonical record shape, or None if unusable."""
    timestamp = parse_timestamp(row.get(mapping["created_utc"]))
    if timestamp is None:
        return None

    body = (row.get(mapping["body"]) or "").strip()
    if not body:
        return None

    record: dict[str, Any] = {
        # Not every dump carries an id; a stable synthetic one keeps
        # de-duplication working without pretending it came from Reddit.
        "id": (row.get(mapping["id"]) or f"{source}:{row_number}") if "id" in mapping
        else f"{source}:{row_number}",
        "created_utc": timestamp,
        "body": body,
        "_source": source,
    }
    for optional in ("author", "score", "subreddit", "link_id"):
        if optional in mapping:
            value = row.get(mapping[optional])
            if value not in (None, ""):
                record[optional] = value

    if "score" in record:
        try:
            record["score"] = int(float(record["score"]))
        except (TypeError, ValueError):
            del record["score"]

    return record


def ingest_csv(
    path: Path,
    store: RawStore,
    source: str,
    dataset_prefix: str = "kaggle",
    batch_size: int = 50_000,
) -> IngestReport:
    """Stream a CSV into the raw archive under its own per-source path.

    Streamed rather than loaded: these files run to gigabytes, and holding one in
    memory would fail on exactly the datasets worth having.
    """
    report = IngestReport(source=source)
    dataset = f"{dataset_prefix}/{source}"
    seen: set[str] = set()
    batch: list[dict[str, Any]] = []

    # Some dumps carry very long comment bodies in a single field.
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        mapping = resolve_columns(reader.fieldnames or [])
        report.column_mapping = mapping
        log.info("%s column mapping: %s", source, mapping)

        for row_number, row in enumerate(reader):
            report.rows_read += 1
            record = normalise_row(row, mapping, source, row_number)
            if record is None:
                if parse_timestamp(row.get(mapping["created_utc"])) is None:
                    report.bad_timestamps += 1
                else:
                    report.missing_text += 1
                continue
            if record["id"] in seen:
                report.duplicates += 1
                continue
            seen.add(record["id"])
            batch.append(record)

            if len(batch) >= batch_size:
                report.withdrawn += len(batch)
                kept = filter_withdrawn(batch)
                report.withdrawn -= len(kept)
                store.append(dataset, kept)
                report.rows_written += len(kept)
                batch = []

    if batch:
        report.withdrawn += len(batch)
        kept = filter_withdrawn(batch)
        report.withdrawn -= len(kept)
        store.append(dataset, kept)
        report.rows_written += len(kept)

    return report


def download_dataset(ref: str, dest: Path, unzip: bool = True) -> Path:
    """Download a Kaggle dataset. Credentials come from the environment only."""
    if not (os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY")):
        raise MissingCredentials(
            "Set KAGGLE_USERNAME and KAGGLE_KEY in the environment. "
            "Get them from kaggle.com -> Settings -> API -> Create New Token."
        )
    from kaggle.api.kaggle_api_extended import KaggleApi

    api = KaggleApi()
    api.authenticate()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    log.info("Downloading %s -> %s", ref, dest)
    api.dataset_download_files(ref, path=str(dest), unzip=unzip, quiet=False)
    return dest
