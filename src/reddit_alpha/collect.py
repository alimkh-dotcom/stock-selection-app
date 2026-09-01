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


# The search term for each thread type. Kept separate from the verification
# patterns above: the query is what the API matches loosely, the pattern is what
# we hold the title to afterwards.
QUERY_FOR_THREAD_TYPE = {
    "moves_tomorrow": "what are your moves tomorrow",
    "daily_discussion": "daily discussion thread",
    "weekend_discussion": "weekend discussion thread",
    "weekly_tendies": "weekly tendies thread",
}


def month_windows(start: date, end: date, months: int = 1) -> Iterator[tuple[date, date]]:
    """Yield [window_start, window_end) windows of ``months`` covering [start, end).

    Wider windows mean far fewer requests: pagination reads 100 results per
    request either way, so a year-wide window costs about five requests where
    twelve month-wide windows cost twelve. Rate limit is the binding constraint
    here, so the width is worth tuning.
    """
    current = start.replace(day=1)
    while current < end:
        nxt = current
        for _ in range(months):
            nxt = (nxt.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield max(current, start), min(nxt, end)
        current = nxt


def dedupe_threads(threads: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep one thread per (day, type).

    Reposts and user posts that happen to match leave several candidates on some
    days. The official thread is stickied or posted by a distinguished account,
    and failing that is the one with the most engagement, so prefer in that
    order. Returns the kept threads and how many were dropped, because a
    surprising drop count would mean the heuristic is misfiring.
    """
    best: dict[tuple[str, str], dict[str, Any]] = {}
    dropped = 0
    for thread in threads:
        day = datetime.fromtimestamp(thread["created_utc"], timezone.utc).date().isoformat()
        key = (day, thread["thread_type"])
        rank = (
            bool(thread.get("stickied")),
            bool(thread.get("distinguished")),
            thread.get("score", 0),
        )
        if key not in best:
            best[key] = thread
            best[key]["_rank"] = rank
        else:
            dropped += 1
            if rank > best[key]["_rank"]:
                best[key] = thread
                best[key]["_rank"] = rank
    kept = sorted(best.values(), key=lambda t: t["created_utc"])
    for t in kept:
        t.pop("_rank", None)
    return kept, dropped


def day_range(start: date, end: date) -> Iterator[date]:
    current = start
    while current < end:
        yield current
        current += timedelta(days=1)


@dataclass
class CrawlStats:
    units_done: int = 0
    units_failed: int = 0
    units_truncated: int = 0
    records: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "units_done": self.units_done,
            "units_failed": self.units_failed,
            "units_truncated": self.units_truncated,
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

    def discover_daily_threads(
        self,
        start: date,
        end: date,
        subreddit: str = "wallstreetbets",
        window_months: int = 12,
    ) -> list[dict[str, Any]]:
        """Find daily threads by searching titles directly.

        The alternative -- scanning every submission and filtering -- costs about
        nine hours for this subreddit's full history. A targeted search costs one
        request per pattern per month, roughly ten minutes, because it returns
        only matching posts.

        The search also matches body text, so every hit is re-checked against the
        title patterns; measurement put the false-positive rate around 7%.
        """
        found: dict[str, dict[str, Any]] = {}

        for kind, _ in THREAD_PATTERNS:
            query = QUERY_FOR_THREAD_TYPE[kind]
            for window_start, window_end in month_windows(start, end, window_months):
                unit = f"discover/{subreddit}/{kind}/{window_start.isoformat()}"
                try:
                    hits = list(
                        self.client.search_posts(
                            subreddit=subreddit,
                            query=query,
                            after=window_start.isoformat(),
                            before=window_end.isoformat(),
                        )
                    )
                except ArcticShiftError as exc:
                    log.error("Discovery failed %s: %s", unit, exc)
                    self.manifest.note_issue(unit, "discovery_failed", str(exc))
                    continue

                for post in hits:
                    # The query matches body text too, so confirm on the title.
                    if classify_thread(post.get("title", "")) != kind:
                        continue
                    found[post["id"]] = {
                        "id": post["id"],
                        "title": post["title"],
                        "created_utc": post["created_utc"],
                        "thread_type": kind,
                        "subreddit": subreddit,
                        "author": post.get("author"),
                        "score": post.get("score", 0),
                        "stickied": bool(post.get("stickied")),
                        "distinguished": post.get("distinguished"),
                    }
                log.info("%s -> %d threads", unit, len(hits))

        threads = sorted(found.values(), key=lambda t: t["created_utc"])
        return threads

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

    def crawl_thread_comments(
        self,
        threads: list[dict[str, Any]],
        max_per_thread: int | None = None,
    ) -> CrawlStats:
        """Stage 3: comments on each daily thread, one thread per unit.

        ``max_per_thread`` caps how many comments are taken from each thread.
        The API returns at most 100 records per request at roughly 1.5s each,
        which puts an exhaustive crawl of every daily thread far beyond what is
        practical; a cap trades completeness for a dataset that can actually be
        assembled.

        Comments arrive oldest first, so a cap keeps the earliest ones. For the
        "what are your moves tomorrow" threads that is the most decision-relevant
        window -- they are posted after the close and the early replies are the
        forward-looking ones -- but it *is* a bias, and every truncated thread is
        recorded in the manifest so its extent can be measured rather than
        assumed. Sensitivity to the cap should be checked before trusting any
        result that depends on it.
        """
        stats = CrawlStats()

        for thread in threads:
            unit = f"comments/{thread['id']}"
            if self.manifest.is_done(unit):
                continue

            try:
                records = list(
                    self.client.thread_comments(thread["id"], limit=max_per_thread)
                )
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

            if max_per_thread is not None and len(records) >= max_per_thread:
                # Hit the cap, so this thread almost certainly has more. Record
                # it: a later analysis must be able to tell a fully-read thread
                # from a truncated one.
                self.manifest.note_issue(
                    unit, "thread_truncated", f"capped at {max_per_thread}"
                )
                stats.units_truncated += 1

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
