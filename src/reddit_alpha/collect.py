"""Crawl orchestration.

Staged so that no subreddit is scanned twice:

  Stage 1  every submission in every subreddit          -> Track B, and the
                                                           index Stage 2 reads
  Stage 2  pick the daily discussion threads out of it   (no network calls)
  Stage 3  every comment on those threads               -> Track A

Track A originally implied its own crawl, but the daily threads are just
submissions, so Stage 1 already retrieves them. Deriving the thread list from
stored posts costs nothing and keeps the two tracks provably consistent.

Work is committed one day at a time. A day is only marked done once its records
are on disk, so an interrupted crawl resumes at the first incomplete day and
never double-writes a finished one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

from .arctic import ArcticShiftClient, ArcticShiftError
from .fields import project_comment, project_post
from .storage import Manifest, RawStore

log = logging.getLogger(__name__)

SUBREDDITS = [
    "wallstreetbets",
    "stocks",
    "investing",
    "StockMarket",
    "options",
    "pennystocks",
    "ValueInvesting",
    "SecurityAnalysis",
]

# Discovered empirically -- the naming changed repeatedly, and the poster changed
# too (AutoModerator in 2017-2021, OPINION_IS_UNPOPULAR by 2023), so matching on
# author would silently lose whole years. Title matching survives both changes.
THREAD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("moves_tomorrow", re.compile(r"what are your moves tomorrow", re.I)),
    ("daily_discussion", re.compile(r"daily discussion thread", re.I)),
    ("weekend_discussion", re.compile(r"weekend discussion thread", re.I)),
    ("weekly_tendies", re.compile(r"weekly tendies thread", re.I)),
]


def classify_thread(title: str) -> str | None:
    """Return the daily-thread type for ``title``, or None if it is a normal post.

    ``moves_tomorrow`` is the one that matters most: it is posted after the close
    and asks explicitly about the next session, so a comment in it is a genuine
    forward-looking statement. The others are intraday chatter and can only ever
    be read as predicting the *following* session.
    """
    for kind, pattern in THREAD_PATTERNS:
        if pattern.search(title):
            return kind
    return None


def day_range(start: date, end: date) -> Iterator[date]:
    current = start
    while current < end:
        yield current
        current += timedelta(days=1)


@dataclass
class CrawlStats:
    units_done: int = 0
    units_failed: int = 0
    records: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "units_done": self.units_done,
            "units_failed": self.units_failed,
            "records": self.records,
        }


class Collector:
    def __init__(self, client: ArcticShiftClient, store: RawStore, manifest: Manifest) -> None:
        self.client = client
        self.store = store
        self.manifest = manifest

    def crawl_posts(
        self,
        subreddits: list[str],
        start: date,
        end: date,
    ) -> CrawlStats:
        """Stage 1: every submission in each subreddit, one day per unit."""
        stats = CrawlStats()

        for subreddit in subreddits:
            for day in day_range(start, end):
                unit = f"posts/{subreddit}/{day.isoformat()}"
                if self.manifest.is_done(unit):
                    continue

                try:
                    records = list(
                        self.client.search_posts(
                            subreddit=subreddit,
                            after=day.isoformat(),
                            before=(day + timedelta(days=1)).isoformat(),
                        )
                    )
                except ArcticShiftError as exc:
                    # One bad day must not abort a multi-hour crawl, but it also
                    # must not vanish -- it is recorded and reported at the end.
                    log.error("Failed %s: %s", unit, exc)
                    self.manifest.note_issue(unit, "post_fetch_failed", str(exc))
                    stats.units_failed += 1
                    continue

                records = [project_post(rec) for rec in records]
                for rec in records:
                    rec["_subreddit"] = subreddit
                self.store.append(f"posts/{subreddit}", records)
                self.manifest.mark_done(unit, "posts", len(records))
                stats.units_done += 1
                stats.records += len(records)
                log.info("%s -> %d posts", unit, len(records))

        return stats

    def find_daily_threads(self, subreddit: str = "wallstreetbets") -> list[dict[str, Any]]:
        """Stage 2: pick daily threads out of already-stored posts. No network."""
        threads = []
        for post in self.store.read(f"posts/{subreddit}"):
            kind = classify_thread(post.get("title", ""))
            if kind:
                threads.append(
                    {
                        "id": post["id"],
                        "title": post["title"],
                        "created_utc": post["created_utc"],
                        "thread_type": kind,
                        "subreddit": subreddit,
                        "author": post.get("author"),
                    }
                )
        threads.sort(key=lambda t: t["created_utc"])
        return threads

    def crawl_thread_comments(self, threads: list[dict[str, Any]]) -> CrawlStats:
        """Stage 3: every comment on each daily thread, one thread per unit."""
        stats = CrawlStats()

        for thread in threads:
            unit = f"comments/{thread['id']}"
            if self.manifest.is_done(unit):
                continue

            try:
                records = list(self.client.thread_comments(thread["id"]))
            except ArcticShiftError as exc:
                log.error("Failed %s: %s", unit, exc)
                self.manifest.note_issue(unit, "comment_fetch_failed", str(exc))
                stats.units_failed += 1
                continue

            records = [project_comment(rec) for rec in records]
            for rec in records:
                rec["_thread_type"] = thread["thread_type"]
                rec["_thread_id"] = thread["id"]
                rec["_subreddit"] = thread["subreddit"]

            self.store.append(f"comments/{thread['thread_type']}", records)
            self.manifest.mark_done(unit, "comments", len(records))
            stats.units_done += 1
            stats.records += len(records)
            log.info(
                "%s (%s) -> %d comments",
                datetime.fromtimestamp(thread["created_utc"], timezone.utc).date(),
                thread["thread_type"],
                len(records),
            )

        return stats
