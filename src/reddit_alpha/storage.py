"""Raw storage and crawl bookkeeping.

Raw Reddit records are written once and never rewritten. Scoring decisions get
revised constantly; re-crawling four years of history because we changed our mind
about sentiment weighting would not be acceptable, so the archive stays untouched
and every derived table is rebuilt from it.

Records land in gzipped JSON Lines, partitioned by month, which appends cheaply
and survives a crash mid-write without corrupting earlier data.

A SQLite manifest records which units of work are finished so an interrupted
crawl resumes where it stopped rather than starting over.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS completed_units (
    unit_key    TEXT PRIMARY KEY,
    track       TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    completed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crawl_issues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_key    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT,
    noted_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_units_track ON completed_units(track);
"""


def month_of(created_utc: int) -> str:
    return datetime.fromtimestamp(created_utc, timezone.utc).strftime("%Y-%m")


class RawStore:
    """Append-only archive of raw Reddit records."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, dataset: str, month: str) -> Path:
        p = self.root / dataset / f"{month}.jsonl.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def append(self, dataset: str, records: Iterable[dict[str, Any]]) -> int:
        """Append records, partitioning by the month they were created in."""
        by_month: dict[str, list[dict[str, Any]]] = {}
        for rec in records:
            by_month.setdefault(month_of(rec["created_utc"]), []).append(rec)

        written = 0
        for month, batch in by_month.items():
            with gzip.open(self._path(dataset, month), "at", encoding="utf-8") as fh:
                for rec in batch:
                    fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
                    fh.write("\n")
                    written += 1
        return written

    def read(self, dataset: str, month: str | None = None) -> Iterator[dict[str, Any]]:
        directory = self.root / dataset
        if not directory.exists():
            return
        files = (
            [directory / f"{month}.jsonl.gz"] if month else sorted(directory.glob("*.jsonl.gz"))
        )
        for path in files:
            if not path.exists():
                continue
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        yield json.loads(line)

    def months(self, dataset: str) -> list[str]:
        directory = self.root / dataset
        if not directory.exists():
            return []
        return sorted(p.name.removesuffix(".jsonl.gz") for p in directory.glob("*.jsonl.gz"))


class Manifest:
    """Tracks completed crawl units so an interrupted run can resume."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def is_done(self, unit_key: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM completed_units WHERE unit_key = ?", (unit_key,)
            ).fetchone()
        return row is not None

    def done_keys(self, track: str) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT unit_key FROM completed_units WHERE track = ?", (track,)
            ).fetchall()
        return {r[0] for r in rows}

    def mark_done(self, unit_key: str, track: str, record_count: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO completed_units "
                "(unit_key, track, record_count, completed_at) VALUES (?, ?, ?, ?)",
                (unit_key, track, record_count, datetime.now(timezone.utc).isoformat()),
            )

    def note_issue(self, unit_key: str, kind: str, detail: str = "") -> None:
        """Record a problem without stopping the crawl.

        Gaps and failures need to survive to the final report; a crawl that
        quietly skipped a week would make later results impossible to trust.
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO crawl_issues (unit_key, kind, detail, noted_at) "
                "VALUES (?, ?, ?, ?)",
                (unit_key, kind, detail, datetime.now(timezone.utc).isoformat()),
            )

    def issues(self) -> list[tuple[str, str, str, str]]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT unit_key, kind, detail, noted_at FROM crawl_issues ORDER BY id"
            ).fetchall()

    def summary(self) -> dict[str, dict[str, int]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT track, COUNT(*), COALESCE(SUM(record_count), 0) "
                "FROM completed_units GROUP BY track"
            ).fetchall()
        return {track: {"units": units, "records": records} for track, units, records in rows}
