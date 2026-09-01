"""Client for the Arctic Shift Reddit archive.

Two API behaviours drive the design here:

1. The service rate-limits on a 60s window and answers overload with HTTP 422
   and ``{"error": "Timeout. Maybe slow down a bit"}``. That is a *soft* signal,
   not a hard failure, so the client paces itself and backs off adaptively.

2. ``after`` filters on ``created_utc`` and is **exclusive**. Paging with
   ``after=<last timestamp on page>`` therefore drops every record that shares
   that second with the last one. We page with ``after=<last timestamp> - 1``
   and de-duplicate by id instead, which re-reads the boundary second rather
   than losing part of it.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://arctic-shift.photon-reddit.com/api"

# The API caps page size at 100; asking for more is rejected outright.
MAX_PAGE = 100


class ArcticShiftError(RuntimeError):
    """Raised when a request cannot be completed after exhausting retries."""


@dataclass
class RateLimiter:
    """Adaptive pacing.

    Starts at ``min_interval`` and slows down whenever the server pushes back,
    recovering gradually after a run of clean responses. The server's limits are
    undocumented, so the safe move is to discover them by observation rather
    than hard-code a guess.
    """

    min_interval: float = 1.2
    max_interval: float = 30.0
    interval: float = 1.2
    _last_call: float = field(default=0.0, repr=False)
    _clean_streak: int = field(default=0, repr=False)

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_call = time.monotonic()

    def penalise(self) -> None:
        self._clean_streak = 0
        self.interval = min(self.interval * 2.0, self.max_interval)
        log.warning("Backing off: request interval now %.1fs", self.interval)

    def reward(self) -> None:
        self._clean_streak += 1
        # Only speed up after sustained success, and only gently.
        if self._clean_streak >= 20 and self.interval > self.min_interval:
            self.interval = max(self.interval * 0.8, self.min_interval)
            self._clean_streak = 0
            log.info("Recovered: request interval now %.1fs", self.interval)


class ArcticShiftClient:
    def __init__(
        self,
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 6,
        timeout: int = 90,
        user_agent: str = "reddit-alpha-research/0.1",
    ) -> None:
        self.limiter = rate_limiter or RateLimiter()
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent

    def _request(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        last_error: str | None = None

        for attempt in range(self.max_retries):
            self.limiter.wait()
            try:
                resp = self.session.get(
                    f"{BASE_URL}/{path}", params=params, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = f"transport: {exc}"
                self.limiter.penalise()
                self._sleep_backoff(attempt)
                continue

            # The service signals overload with 422 and a JSON error body rather
            # than 429, so status alone is not enough to classify the response.
            payload: Any = None
            try:
                payload = resp.json()
            except ValueError:
                payload = None

            if isinstance(payload, dict) and payload.get("data") is not None:
                self.limiter.reward()
                return payload["data"]

            err = (payload or {}).get("error") if isinstance(payload, dict) else None
            last_error = f"http {resp.status_code}: {err or resp.text[:120]}"

            if resp.status_code == 400:
                # A malformed query will never succeed; retrying just wastes the
                # rate-limit budget.
                raise ArcticShiftError(f"{path} rejected the query: {last_error}")

            self.limiter.penalise()
            self._sleep_backoff(attempt, resp.headers.get("x-ratelimit-reset"))

        raise ArcticShiftError(
            f"{path} failed after {self.max_retries} attempts: {last_error}"
        )

    def _sleep_backoff(self, attempt: int, reset_hint: str | None = None) -> None:
        if reset_hint:
            try:
                # The server tells us when the window resets; honour it.
                time.sleep(min(float(reset_hint) + 1, 90))
                return
            except (TypeError, ValueError):
                pass
        time.sleep(min(2**attempt, 60) + random.uniform(0, 1))

    def paginate(
        self,
        path: str,
        params: dict[str, Any],
        stop_after: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every record matching ``params``, oldest first.

        De-duplicates by id because the one-second overlap described in the
        module docstring deliberately re-reads the page boundary.
        """
        seen: set[str] = set()
        cursor = params.get("after")
        emitted = 0

        while True:
            page_params = dict(params, limit=MAX_PAGE, sort="asc")
            if cursor is not None:
                page_params["after"] = cursor

            page = self._request(path, page_params)
            if not page:
                return

            fresh = [rec for rec in page if rec["id"] not in seen]
            for rec in fresh:
                seen.add(rec["id"])
                yield rec
                emitted += 1
                if stop_after is not None and emitted >= stop_after:
                    return

            last_ts = page[-1]["created_utc"]

            if not fresh and len(page) == MAX_PAGE:
                # Every record on a full page was already seen: more than
                # MAX_PAGE records share this second, so the one-second overlap
                # can no longer advance. Ascending order can only ever reach the
                # first MAX_PAGE of them -- but descending order reaches the
                # last MAX_PAGE, so read the second from both ends before
                # conceding that anything is lost.
                for rec in self._read_second_backwards(path, params, last_ts, seen):
                    yield rec
                    emitted += 1
                    if stop_after is not None and emitted >= stop_after:
                        return
                cursor = last_ts
                continue

            if len(page) < MAX_PAGE:
                return

            # Overlap by one second so a page ending mid-second is not truncated.
            cursor = last_ts - 1


    def _read_second_backwards(
        self,
        path: str,
        params: dict[str, Any],
        timestamp: int,
        seen: set[str],
    ) -> list[dict[str, Any]]:
        """Recover the tail of a second that ascending pagination cannot reach.

        Ascending order yields the first MAX_PAGE records of a crowded second and
        then stalls. Descending order yields the last MAX_PAGE. Together they
        cover any second holding up to 2 * MAX_PAGE records; beyond that the API
        offers no cursor that could reach the middle, and the gap is real and
        must be reported rather than hidden.
        """
        tail_params = dict(
            params,
            after=timestamp - 1,
            before=timestamp + 1,
            limit=MAX_PAGE,
            sort="desc",
        )
        tail = self._request(path, tail_params)
        recovered = [rec for rec in tail if rec["id"] not in seen]
        for rec in recovered:
            seen.add(rec["id"])

        if len(tail) == MAX_PAGE and len(recovered) == MAX_PAGE:
            # The two ends did not meet, so records sit between them unreachable.
            log.error(
                "Unrecoverable gap at created_utc=%s for %s: the second holds "
                "more than %d records and the API exposes no cursor within a "
                "second. RECORDS ARE LOST HERE.",
                timestamp,
                params,
                2 * MAX_PAGE,
            )
        else:
            log.warning(
                "Crowded second at created_utc=%s: recovered %d extra records "
                "by reading backwards.",
                timestamp,
                len(recovered),
            )
        return recovered

    def search_posts(self, **params: Any) -> Iterator[dict[str, Any]]:
        return self.paginate("posts/search", params)

    def search_comments(self, **params: Any) -> Iterator[dict[str, Any]]:
        return self.paginate("comments/search", params)

    def thread_comments(
        self, post_id: str, limit: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Comments on one submission, oldest first.

        ``post_id`` is the bare id with no ``t3_`` prefix. ``limit`` caps how
        many are read; see ``Collector.crawl_thread_comments`` for why capping
        is usually necessary and what it costs.
        """
        return self.paginate(
            "comments/search", {"link_id": f"t3_{post_id}"}, stop_after=limit
        )
