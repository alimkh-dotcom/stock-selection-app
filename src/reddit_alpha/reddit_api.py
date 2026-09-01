"""Collection through Reddit's own API.

The public archive throttles too hard to assemble this dataset, so authenticated
Reddit access is the practical route. Credentials are read from the environment
and never from a file in the repository.

Two differences from the archive matter enough that they must not be glossed
over when the two sources are compared or combined.

**Deleted content is gone.** Reddit serves ``[deleted]`` and ``[removed]`` in
place of text that has since been taken down, while the archive captured it at
posting time. In a 2023 sample, 26% of comment authors were already ``[deleted]``.
This is a different bias, not an absence of one, so removed comments are counted
and reported rather than quietly dropped.

**Ordering is by rank, not time.** The archive returns comments oldest first, so
a cap keeps the earliest. Reddit returns them in the site's own ranking, so a cap
keeps the *most engaged with*. Neither is wrong, but they are not the same
sample, and a comparison between the two sources that ignored this would be
measuring the sampling rule as much as the source.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterator

log = logging.getLogger(__name__)

USER_AGENT = "python:reddit-alpha-research:0.1 (by /u/{username})"

DELETED_MARKERS = frozenset({"[deleted]", "[removed]"})


class MissingCredentials(RuntimeError):
    """Raised when the Reddit API credentials are not present in the environment."""


@dataclass
class CollectionStats:
    threads: int = 0
    comments: int = 0
    deleted_bodies: int = 0
    truncated_threads: int = 0
    failed_threads: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "threads": self.threads,
            "comments": self.comments,
            "deleted_bodies": self.deleted_bodies,
            "deleted_share": self.deleted_bodies / self.comments if self.comments else 0.0,
            "truncated_threads": self.truncated_threads,
            "failed_threads": self.failed_threads,
        }


def build_client(username: str | None = None):
    """Create a read-only Reddit client from environment credentials.

    Read-only (application-only OAuth) is enough: everything here is public
    content, and it avoids handling an account password.
    """
    import praw

    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise MissingCredentials(
            "Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET in the environment. "
            "Create a 'script' app at https://www.reddit.com/prefs/apps"
        )

    username = username or os.environ.get("REDDIT_USERNAME", "researcher")
    client = praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=USER_AGENT.format(username=username),
        check_for_async=False,
    )
    client.read_only = True
    return client


def _to_record(comment: Any, thread_id: str, thread_type: str, subreddit: str) -> dict[str, Any]:
    """Project a PRAW comment into the same shape the archive produces.

    Keeping one schema means the extraction, signal and backtest code never
    needs to know which source a record came from.
    """
    author = getattr(comment, "author", None)
    return {
        "id": comment.id,
        "created_utc": int(comment.created_utc),
        "author": str(author) if author else "[deleted]",
        "body": comment.body,
        "score": comment.score,
        "link_id": f"t3_{thread_id}",
        "parent_id": getattr(comment, "parent_id", None),
        "subreddit": subreddit,
        "is_submitter": getattr(comment, "is_submitter", None),
        "_thread_id": thread_id,
        "_thread_type": thread_type,
        "_subreddit": subreddit,
        "_source": "reddit_api",
    }


class RedditCollector:
    def __init__(self, client: Any, expand_batches: int = 8) -> None:
        """``expand_batches`` bounds how far the comment tree is expanded.

        Reddit returns a tree with unexpanded "more comments" nodes, and each
        expansion is another request. Unbounded expansion on a thread with tens
        of thousands of comments costs hundreds of requests, so the depth of
        expansion is capped and reported.
        """
        self.client = client
        self.expand_batches = expand_batches
        self.stats = CollectionStats()

    def thread_comments(
        self, thread_id: str, thread_type: str, subreddit: str, max_comments: int | None = None
    ) -> list[dict[str, Any]]:
        submission = self.client.submission(id=thread_id)
        submission.comments.replace_more(limit=self.expand_batches)

        records: list[dict[str, Any]] = []
        for comment in submission.comments.list():
            if max_comments is not None and len(records) >= max_comments:
                self.stats.truncated_threads += 1
                break
            record = _to_record(comment, thread_id, thread_type, subreddit)
            if record["body"] in DELETED_MARKERS:
                # Kept, not dropped: the count is the measure of how much this
                # source is missing relative to the archive.
                self.stats.deleted_bodies += 1
            records.append(record)

        self.stats.threads += 1
        self.stats.comments += len(records)
        return records


def compare_sources(
    archive_records: list[dict[str, Any]], api_records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Measure how much the two sources actually differ on the same thread.

    Run before mixing them. Deleted content and ranking order both pull the two
    samples apart, and the size of that gap should be a number rather than an
    assumption.
    """
    archive_ids = {r["id"] for r in archive_records}
    api_ids = {r["id"] for r in api_records}
    overlap = archive_ids & api_ids

    api_by_id = {r["id"]: r for r in api_records}
    lost_text = sum(
        1
        for r in archive_records
        if r["id"] in overlap
        and r.get("body") not in DELETED_MARKERS
        and api_by_id[r["id"]].get("body") in DELETED_MARKERS
    )

    return {
        "archive_only": len(archive_ids - api_ids),
        "api_only": len(api_ids - archive_ids),
        "in_both": len(overlap),
        "overlap_share": len(overlap) / len(archive_ids | api_ids) if (archive_ids | api_ids) else 0.0,
        "text_lost_to_deletion": lost_text,
    }
