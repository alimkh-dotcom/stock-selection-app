"""Honouring deletion.

Someone who deletes a Reddit comment has withdrawn it. A local copy that
outlives that act quietly defeats the deletion, so this module removes such
content from the archive rather than leaving it to be noticed later.

Two mechanisms, because deletion can be learned two ways:

**At ingest.** Content already showing ``[deleted]`` or ``[removed]`` is dropped
before it is ever written. Cheap, and it needs nothing but the record itself.

**On refresh.** Content that was live when collected may be deleted afterwards.
Detecting that requires re-checking against a live source, so ``purge_deleted``
takes a callable that reports which ids are still present and rewrites the
archive without the rest.

Deletions are counted, not silently applied. A study that loses a large fraction
of its corpus this way has a coverage problem worth knowing about -- 26% of
authors in a 2023 sample were already deleted -- and the count is the only way
to see it.
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

# What Reddit serves in place of withdrawn text.
DELETION_MARKERS = frozenset({"[deleted]", "[removed]"})

# Author handles carry the same markers when an account is gone.
DELETED_AUTHORS = frozenset({"[deleted]", "[removed]"})


@dataclass
class RetentionReport:
    scanned: int = 0
    dropped_at_ingest: int = 0
    dropped_on_refresh: int = 0
    files_rewritten: list[str] = field(default_factory=list)

    @property
    def dropped(self) -> int:
        return self.dropped_at_ingest + self.dropped_on_refresh

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "dropped_at_ingest": self.dropped_at_ingest,
            "dropped_on_refresh": self.dropped_on_refresh,
            "dropped_share": self.dropped / self.scanned if self.scanned else 0.0,
            "files_rewritten": self.files_rewritten,
        }


def is_withdrawn(record: dict[str, Any]) -> bool:
    """Whether this record's content has been withdrawn by its author or a mod."""
    body = record.get("body")
    if body is not None and body.strip() in DELETION_MARKERS:
        return True
    # Submissions carry their text in selftext instead.
    selftext = record.get("selftext")
    if selftext is not None and selftext.strip() in DELETION_MARKERS:
        return True
    return False


def filter_withdrawn(
    records: Iterable[dict[str, Any]], report: RetentionReport | None = None
) -> list[dict[str, Any]]:
    """Drop already-withdrawn content before it is stored.

    A deleted account (``author`` of ``[deleted]``) is *not* by itself grounds to
    drop the record: the comment text is still public on Reddit, and the handle
    is only ever used to count distinct participants. Removing those would
    discard live content and distort the counts.
    """
    kept = []
    for record in records:
        if report is not None:
            report.scanned += 1
        if is_withdrawn(record):
            if report is not None:
                report.dropped_at_ingest += 1
            continue
        kept.append(record)
    return kept


def purge_deleted(
    raw_root: Path,
    dataset: str,
    still_present: Callable[[list[str]], set[str]] | None = None,
    batch_size: int = 100,
) -> RetentionReport:
    """Rewrite ``dataset`` without content that has since been withdrawn.

    ``still_present`` receives a batch of record ids and returns the subset that
    is still live. Passing None applies only the marker check, which is what can
    be done without a live source.

    Files are rewritten via a temporary file and swapped into place, so an
    interruption cannot leave the archive half-written.
    """
    report = RetentionReport()
    directory = Path(raw_root) / dataset
    if not directory.exists():
        return report

    for path in sorted(directory.glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]

        report.scanned += len(records)
        kept = [r for r in records if not is_withdrawn(r)]
        report.dropped_at_ingest += len(records) - len(kept)

        if still_present is not None and kept:
            live: set[str] = set()
            for start in range(0, len(kept), batch_size):
                batch = [r["id"] for r in kept[start : start + batch_size]]
                live |= still_present(batch)
            before = len(kept)
            kept = [r for r in kept if r["id"] in live]
            report.dropped_on_refresh += before - len(kept)

        if len(kept) == len(records):
            continue

        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            for record in kept:
                fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                fh.write("\n")
        tmp.replace(path)
        report.files_rewritten.append(path.name)
        log.info("Purged %d withdrawn records from %s", len(records) - len(kept), path.name)

    return report
