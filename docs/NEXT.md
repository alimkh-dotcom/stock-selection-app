# Resuming this project

Written at a deliberate pause: a Reddit Data API access request has been
submitted and the work is waiting on approval. This container is ephemeral, so
this file — not the machine — is the handoff.

## Where things stand

The analysis pipeline is built and tested. Data collection is the only blocker.

| Component | State |
|---|---|
| Archive collector, resumable, gap-reporting | done |
| Reddit API collector | done, awaiting credentials |
| Daily-thread discovery, all title eras 2017→now | done |
| Ticker extraction | done, ~88% precision hand-measured |
| Price layer, point-in-time and survivorship-aware | done |
| Lead/lag event study | done, validated on planted signals |
| Patient crawler for a throttled source | done |
| **Data collection at scale** | **blocked** |
| Lead/lag result (the gate) | not started — needs data |

~135 tests: `python3 -m pytest tests/ -q`

Nothing collected so far survives this session. That is fine: it was a few
thousand comments used to shake out bugs, and it did its job — it caught the
`BLSH`/"bullish" collision, the `name_context` method scoring 0 for 4, the
`RDDT` anachronism, and the SOFI-marked-as-delisted bug.

## Do this first, before any collection

**Deletion honouring is implemented** (`src/reddit_alpha/retention.py`). Content
already withdrawn is dropped before it is ever stored, in both collectors, and
`purge_deleted()` rewrites an existing archive without content deleted since
collection. The API request's commitment is now backed by code and tests.

**Keep the author track-record feature out of v1.** The request says no
per-user output is produced. Scoring individual users by past accuracy would
contradict that. Revisit only after the gate says there is signal worth
refining, and update Reddit if the answer changes.

## On approval

1. Set credentials in the environment — never in a file in this repo:
   ```
   export REDDIT_CLIENT_ID=...
   export REDDIT_CLIENT_SECRET=...
   ```
2. Verify real throughput on a handful of threads before committing to a plan.
   The whole schedule below assumes Reddit is materially faster than the
   archive; confirm that rather than assuming it.
3. Run `compare_sources()` on threads collected both ways. Reddit does not serve
   deleted or removed comment text that the archive captured at posting time,
   and 26% of authors in a 2023 sample were already `[deleted]`. That gap should
   be a measured number before the two sources are mixed or compared.
4. Collect the pilot: ~200 daily threads, capped at ~1,000 comments each.
5. Run the lead/lag event study. **This is the gate.**
6. Commit the collected data to the repository. At ~250 MB it fits, and it makes
   the work survive session boundaries without any cloud storage.

## If the archive is the only route

It answered roughly 1 request in 6 at 12-second spacing, and the budget appears
to refill over a long window. Patient mode is built for exactly this:

```
python3 scripts/crawl.py discover --subreddits wallstreetbets \
    --start 2021-01-01 --end 2024-01-01 --data-dir data
python3 scripts/crawl.py comments --patient --max-comments 1000 \
    --thread-types moves_tomorrow --budget-minutes 60 --data-dir data
```

Both stages resume. Discovery checkpoints between windows; comment collection
commits per thread.

## Reading the gate

Take every attention spike, align at day zero, average market-adjusted returns
from 20 days before to 20 after.

- Returns rising **before** day zero → Reddit follows price. **Report it and
  stop.** The strategy does not exist, and that is a real finding cheaply
  obtained.
- Returns rising **after** day zero → there is something. Proceed to the
  quiet-buildup screen and size-bucket breakdown.

Resist the urge to reach for a different threshold or window if the first
answer is unwelcome. Every such choice is made on training and validation data
only; 2024 onward stays sealed and is touched exactly once.

## Context worth not relearning

- Discovery matches on **author**, not title search: faster, less throttled, and
  it found 45 threads to the title search's 29 for January 2021.
- The daily thread's posting account has changed once already, so
  `coverage_report()` checks output against the trading calendar. Heed it.
- Ticker ambiguity is decided by word frequency, not a hand-written list. The
  hand-written list failed on real data within minutes.
- Every measured surprise is logged in `docs/FINDINGS.md`. Read it before
  changing anything in extraction or collection.
