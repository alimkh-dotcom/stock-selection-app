"""The subset of Reddit fields the archive keeps.

Reddit records carry 92 fields, and measurement showed roughly two thirds of the
compressed bytes go to things this project can never use: award listings, media
previews, flair styling, thumbnail geometry. At the scale of this crawl that is
the difference between a manageable archive and an unmanageable one.

The archive is still write-once -- we simply write a documented projection of
each record rather than all of it. The subset is deliberately generous: adding a
field later means re-crawling, so anything with a plausible use is kept even if
nothing reads it today.

``SCHEMA_VERSION`` is written alongside the data. If the subset ever changes,
the version changes with it, so a mixed-vintage archive is detectable rather
than quietly inconsistent.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1

POST_FIELDS = frozenset({
    "id", "created_utc", "author", "author_fullname", "title", "selftext",
    "score", "upvote_ratio", "num_comments", "subreddit", "link_flair_text",
    "is_self", "distinguished", "stickied", "locked", "over_18",
    "removed_by_category", "domain", "url", "permalink",
})

COMMENT_FIELDS = frozenset({
    "id", "created_utc", "author", "author_fullname", "body", "score",
    "link_id", "parent_id", "subreddit", "distinguished", "stickied",
    "controversiality", "is_submitter",
})

# Fields the collector attaches itself; they must survive pruning.
DERIVED_PREFIX = "_"


def project(record: dict[str, Any], keep: frozenset[str]) -> dict[str, Any]:
    """Return the kept fields of ``record``, plus anything we added ourselves."""
    return {
        k: v
        for k, v in record.items()
        if k in keep or k.startswith(DERIVED_PREFIX)
    }


def project_post(record: dict[str, Any]) -> dict[str, Any]:
    return project(record, POST_FIELDS)


def project_comment(record: dict[str, Any]) -> dict[str, Any]:
    return project(record, COMMENT_FIELDS)
