"""Patient runner tests.

Sleeps are faked so the behaviour under a hostile source can be tested in
milliseconds rather than hours.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.patient import PatienceConfig, PatientRunner


class FakeClock:
    def __init__(self):
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)


def runner(**kwargs):
    clock = FakeClock()
    cfg = PatienceConfig(**{"park_seconds": 900, **kwargs})
    return PatientRunner(cfg, sleep=clock), clock


def test_all_units_run_when_the_source_cooperates():
    r, _ = runner()
    stats = r.run(range(10), lambda u: 5)
    assert stats.succeeded == 10
    assert stats.records == 50
    assert stats.stopped_because == "all units attempted"


def test_each_success_is_committed_before_the_next_attempt():
    """An interrupted crawl must lose at most the unit in flight."""
    r, _ = runner()
    committed = []
    attempted = []

    def do(u):
        # Everything committed so far must already be on disk when this starts.
        attempted.append((u, list(committed)))
        return 1

    r.run(range(4), do, on_success=lambda u, n: committed.append(u))
    assert committed == [0, 1, 2, 3]
    assert attempted[3][1] == [0, 1, 2], "a completed unit was not committed in time"


def test_failures_do_not_abort_the_run():
    r, _ = runner()
    def do(u):
        if u % 2:
            raise RuntimeError("refused")
        return 1
    stats = r.run(range(10), do)
    assert stats.succeeded == 5 and stats.failed == 5


def test_interval_widens_under_sustained_refusal():
    r, clock = runner(start_interval=2.0, widen_after=2, park_after=99)
    r.run(range(6), lambda u: (_ for _ in ()).throw(RuntimeError("no")))
    assert clock.slept[-1] > clock.slept[0], "did not slow down while being refused"


def test_interval_narrows_again_when_the_source_recovers():
    r, clock = runner(start_interval=8.0, min_interval=1.0, narrow_after=3)
    r.run(range(10), lambda u: 1)
    assert clock.slept[-1] < clock.slept[0], "stayed slow after the source recovered"


def test_never_faster_than_the_floor():
    """Politeness floor: this is a free shared service."""
    r, clock = runner(start_interval=2.0, min_interval=1.5, narrow_after=1)
    r.run(range(30), lambda u: 1)
    assert min(clock.slept) >= 1.5


def test_parks_instead_of_hammering():
    """Once the budget is gone, more requests only add refusals."""
    r, clock = runner(park_after=3, park_seconds=900, max_parks=99)
    r.run(range(6), lambda u: (_ for _ in ()).throw(RuntimeError("no")))
    assert 900 in clock.slept, "kept hammering instead of backing right off"
    assert r.stats.parks >= 1


def test_gives_up_when_nothing_gets_through():
    """A run that cannot make progress should say so, not spin forever."""
    r, _ = runner(park_after=2, max_parks=2)
    stats = r.run(range(100), lambda u: (_ for _ in ()).throw(RuntimeError("no")))
    assert stats.parks == 2
    assert "refusing everything" in stats.stopped_because
    assert stats.attempted < 100, "kept going after the source was clearly closed"


def test_survives_the_observed_one_in_six_regime():
    """The measured condition was ~1 success in 6 at 12s spacing.

    The give-up rule must be calibrated so that this regime is *worked through*,
    not abandoned -- it is the exact case the runner exists for. The defaults
    park only after 12 consecutive refusals, which a 1-in-6 source rarely
    reaches, and any success resets the counter.
    """
    calls = {"n": 0}
    def do(u):
        calls["n"] += 1
        if calls["n"] % 6 == 0:
            return 1
        raise RuntimeError("refused")

    r, _ = runner()          # defaults: park_after=12, max_parks=4
    stats = r.run(range(60), do)

    assert stats.attempted == 60, "gave up despite making steady progress"
    assert stats.succeeded == 10
    assert stats.parks == 0, "parked despite the source answering regularly"


def test_a_success_resets_the_give_up_counter():
    """Parks only end the run when none of them is followed by progress."""
    calls = {"n": 0}
    def do(u):
        calls["n"] += 1
        # Long refusal runs, but a success after each park.
        if calls["n"] % 5 == 0:
            return 1
        raise RuntimeError("refused")

    r, _ = runner(park_after=4, max_parks=2)
    stats = r.run(range(30), do)
    assert stats.attempted == 30, "gave up despite recovering after each park"


def test_time_budget_stops_the_run_cleanly():
    import time as _time
    r, _ = runner(budget_seconds=0.0)
    stats = r.run(range(50), lambda u: 1)
    assert stats.attempted == 0
    assert stats.stopped_because == "time budget reached"


def test_stats_report_the_success_rate():
    r, _ = runner(park_after=99)
    def do(u):
        if u % 4:
            raise RuntimeError("no")
        return 2
    r.run(range(8), do)
    assert r.stats.as_dict()["success_rate"] == pytest.approx(0.25)
