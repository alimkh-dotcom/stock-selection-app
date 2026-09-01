"""A crawler built for a source that mostly says no.

The archive answers roughly one request in six even at 12-second spacing, and
the budget appears to refill over a long window rather than per minute. A
conventional crawler treats that as failure and gives up; this one treats it as
the normal condition and makes progress whenever the quota allows.

Three properties follow from that:

**It never loses ground.** Every completed unit is committed before the next
begins, so being killed mid-run costs at most one unit.

**It reads the source's mood.** Sustained refusals widen the interval and,
past a point, park the crawl for a long sleep rather than burning quota on
requests that will be refused anyway. Sustained success narrows it again.

**It stops rather than thrashes.** A run that cannot get through at all exits
with a clear report instead of hammering a service that is already saying no.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

log = logging.getLogger(__name__)


@dataclass
class PatienceConfig:
    """Pacing for a source that refuses most requests.

    Defaults are set from measurement, not taste: 70 comments/sec was achievable
    on a fresh budget, and 1-in-6 at 12s once exhausted.
    """

    start_interval: float = 3.0
    min_interval: float = 1.5
    max_interval: float = 60.0
    # Widen after this many consecutive failures.
    widen_after: int = 2
    # Narrow after this many consecutive successes.
    narrow_after: int = 5
    # Park the crawl entirely after this many consecutive failures.
    park_after: int = 12
    park_seconds: float = 900.0
    # Give up after this many parks without a single success.
    max_parks: int = 4
    # Stop cleanly after this long, so a run fits inside a session.
    budget_seconds: float | None = None


@dataclass
class PatientStats:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    parks: int = 0
    records: int = 0
    started_at: float = field(default_factory=time.monotonic)
    stopped_because: str = ""

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "success_rate": self.succeeded / self.attempted if self.attempted else 0.0,
            "parks": self.parks,
            "records": self.records,
            "elapsed_minutes": round(self.elapsed / 60, 1),
            "stopped_because": self.stopped_because,
        }


class PatientRunner:
    """Runs units of work against an uncooperative source, committing as it goes."""

    def __init__(self, config: PatienceConfig | None = None, sleep=time.sleep) -> None:
        self.config = config or PatienceConfig()
        self.interval = self.config.start_interval
        self.stats = PatientStats()
        self._sleep = sleep
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._parks_without_success = 0

    def run(
        self,
        units: Sequence[Any],
        do_unit: Callable[[Any], int],
        on_success: Callable[[Any, int], None] | None = None,
    ) -> PatientStats:
        """Attempt each unit in turn.

        ``do_unit`` returns a record count, or raises to signal failure.
        ``on_success`` commits the unit -- it runs before the next attempt
        starts, so an interrupted crawl loses at most the unit in flight.
        """
        for unit in units:
            if self._out_of_budget():
                self.stats.stopped_because = "time budget reached"
                break
            if self._parks_without_success >= self.config.max_parks:
                self.stats.stopped_because = (
                    f"no success across {self.config.max_parks} parks; "
                    "the source is refusing everything"
                )
                break

            self._sleep(self.interval)
            self.stats.attempted += 1

            try:
                count = do_unit(unit)
            except Exception as exc:  # noqa: BLE001 - any failure is just a refusal
                self._record_failure(exc)
                continue

            self.stats.succeeded += 1
            self.stats.records += count
            self._consecutive_failures = 0
            self._parks_without_success = 0
            self._consecutive_successes += 1
            if on_success is not None:
                on_success(unit, count)
            self._maybe_narrow()
        else:
            self.stats.stopped_because = "all units attempted"

        return self.stats

    def _out_of_budget(self) -> bool:
        budget = self.config.budget_seconds
        return budget is not None and self.stats.elapsed >= budget

    def _record_failure(self, exc: Exception) -> None:
        self.stats.failed += 1
        self._consecutive_successes = 0
        self._consecutive_failures += 1
        log.debug("Unit failed (%d in a row): %s", self._consecutive_failures, exc)

        if self._consecutive_failures >= self.config.park_after:
            # Past this point the budget is clearly gone. Continuing would only
            # add refused requests to a service already saying no.
            log.warning(
                "Parking for %.0f min after %d consecutive failures",
                self.config.park_seconds / 60,
                self._consecutive_failures,
            )
            self.stats.parks += 1
            self._parks_without_success += 1
            self._sleep(self.config.park_seconds)
            self._consecutive_failures = 0
            self.interval = self.config.start_interval
            return

        if self._consecutive_failures % self.config.widen_after == 0:
            self.interval = min(self.interval * 2, self.config.max_interval)
            log.info("Slowing to %.1fs between requests", self.interval)

    def _maybe_narrow(self) -> None:
        if self._consecutive_successes >= self.config.narrow_after:
            self._consecutive_successes = 0
            if self.interval > self.config.min_interval:
                self.interval = max(self.interval * 0.7, self.config.min_interval)
                log.info("Speeding up to %.1fs between requests", self.interval)
