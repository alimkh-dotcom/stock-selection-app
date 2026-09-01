#!/usr/bin/env python3
"""Run a crawl stage. Safe to interrupt and re-run: finished days are skipped.

  discover  find the daily threads by title search (minutes)
  comments  read the comments on those threads          -> Track A
  posts     every submission in each subreddit          -> Track B (hours)
  threads   list daily threads from already-stored posts (no network)

Track A needs only `discover` then `comments`. The full `posts` scan is Track B
and is far more expensive, so it is deliberately not a prerequisite.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.arctic import ArcticShiftClient, RateLimiter
from reddit_alpha.collect import SUBREDDITS, Collector, dedupe_threads
from reddit_alpha.patient import PatienceConfig, PatientRunner
from reddit_alpha.storage import Manifest, RawStore


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["discover", "comments", "posts", "threads"])
    ap.add_argument("--start", type=date.fromisoformat, default=date(2017, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date.today(),
                    help="exclusive")
    ap.add_argument("--subreddits", nargs="*", default=SUBREDDITS)
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--interval", type=float, default=1.2,
                    help="seconds between requests; raised automatically on pushback")
    ap.add_argument("--thread-types", nargs="*", default=None,
                    help="comments stage: restrict to these thread types")
    ap.add_argument("--patient", action="store_true",
                    help="pace for a source that refuses most requests: widen on "
                         "refusal, park when the budget is clearly gone, commit "
                         "each unit as it lands")
    ap.add_argument("--budget-minutes", type=float, default=None,
                    help="stop cleanly after this long, so a run fits a session")
    ap.add_argument("--max-comments", type=int, default=None,
                    help="cap comments taken per thread; truncation is recorded")
    ap.add_argument("--limit-units", type=int, default=None,
                    help="stop after N units; useful for measuring throughput")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    store = RawStore(args.data_dir / "raw")
    manifest = Manifest(args.data_dir / "manifest.db")
    client = ArcticShiftClient(RateLimiter(min_interval=args.interval, interval=args.interval))
    collector = Collector(client, store, manifest)

    started = time.monotonic()

    if args.stage == "discover":
        raw_threads = []
        for sub in args.subreddits:
            raw_threads.extend(collector.discover_daily_threads(args.start, args.end, sub))
        threads, dropped = dedupe_threads(raw_threads)
        out = args.data_dir / "daily_threads.json"
        out.write_text(json.dumps(threads, indent=2))
        by_type: dict[str, int] = {}
        for t in threads:
            by_type[t["thread_type"]] = by_type.get(t["thread_type"], 0) + 1
        span = f"{args.start} to {args.end}"
        print(f"\n{len(threads)} daily threads over {span} -> {out}")
        print(f"({dropped} same-day duplicates dropped)")
        for kind, count in sorted(by_type.items()):
            print(f"  {kind:20s} {count}")
        return 0

    if args.stage == "posts":
        stats = collector.crawl_posts(args.subreddits, args.start, args.end)

    elif args.stage == "threads":
        threads = []
        for sub in args.subreddits:
            threads.extend(collector.find_daily_threads(sub))
        out = args.data_dir / "daily_threads.json"
        out.write_text(json.dumps(threads, indent=2))
        by_type: dict[str, int] = {}
        for t in threads:
            by_type[t["thread_type"]] = by_type.get(t["thread_type"], 0) + 1
        print(f"\n{len(threads)} daily threads -> {out}")
        for kind, count in sorted(by_type.items()):
            print(f"  {kind:20s} {count}")
        return 0

    else:  # comments
        thread_file = args.data_dir / "daily_threads.json"
        if not thread_file.exists():
            print(f"No thread list at {thread_file}.\n"
                  f"Run the discovery stage first:\n"
                  f"  python3 scripts/crawl.py discover --start 2021-01-01 --end 2024-01-01",
                  file=sys.stderr)
            return 2
        threads = json.loads(thread_file.read_text())
        if args.thread_types:
            threads = [t for t in threads if t["thread_type"] in args.thread_types]
        if args.limit_units:
            threads = [t for t in threads if not manifest.is_done(f"comments/{t['id']}")]
            threads = threads[: args.limit_units]
        if args.patient:
            pending = [t for t in threads if not manifest.is_done(f"comments/{t['id']}")]
            runner = PatientRunner(PatienceConfig(
                budget_seconds=args.budget_minutes * 60 if args.budget_minutes else None
            ))
            print(f"patient mode: {len(pending)} threads pending")
            pstats = runner.run(
                pending,
                lambda t: collector.collect_one_thread(t, args.max_comments),
            )
            print(f"\n{'=' * 60}")
            for key, value in pstats.as_dict().items():
                print(f"  {key}: {value}")
            print(f"manifest totals: {manifest.summary()}")
            return 0
        stats = collector.crawl_thread_comments(threads, max_per_thread=args.max_comments)

    elapsed = time.monotonic() - started
    print(f"\n{'=' * 60}")
    print(f"stage={args.stage}  {stats.units_done} units, {stats.records} records, "
          f"{stats.units_failed} failed, {stats.units_truncated} truncated "
          f"in {elapsed / 60:.1f} min")
    if stats.units_done:
        print(f"throughput: {elapsed / stats.units_done:.1f}s per unit, "
              f"{stats.records / stats.units_done:.0f} records per unit")
    issues = manifest.issues()
    if issues:
        print(f"\n{len(issues)} issue(s) recorded -- these affect data completeness:")
        for key, kind, detail, _ in issues[-10:]:
            print(f"  {key}: {kind} {detail[:70]}")
    print(f"manifest totals: {manifest.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
