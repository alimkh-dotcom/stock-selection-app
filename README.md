# Reddit-only stock selection research

Does discussion in retail investing communities **anticipate** stock price moves,
or merely **react** to them?

This project tests that question using public Reddit comments as the only input.
It is a personal, non-commercial research project. Nothing here posts, comments,
votes, messages users, or writes to Reddit in any way — it reads public content
and analyses it offline.

## The question, and why it comes first

People usually post about a stock *because* it jumped. If that is all that
happens, the chatter is a rear-view mirror and there is no strategy to build.

So the first deliverable is not a trading system. It is one chart: take every
moment chatter about a stock spiked, line those moments up at day zero, and
average market-adjusted returns across thousands of them.

- Returns rising **before** day zero → Reddit is late.
- Returns rising **after** day zero → the chatter carried information.

My working hypothesis is the sceptical one — that discussion mostly follows
price. The study is built so that a negative result is a clean outcome that gets
published, not a failure.

## Status

Analysis pipeline built and tested; data collection is the outstanding piece.

| Component | State |
|---|---|
| Archive collector (resumable, gap-reporting) | ✅ |
| Reddit API collector | ✅ awaiting API access |
| Daily-thread discovery (2017→now, all title eras) | ✅ |
| Ticker extraction | ✅ ~88% precision, hand-measured |
| Price layer (point-in-time, survivorship-aware) | ✅ |
| Lead/lag event study | ✅ validated on planted signals |
| Data collection at scale | ⏳ blocked |

~135 automated tests. Run them with `python3 -m pytest tests/ -q`.

## Documentation

- **[docs/SCOPE.md](docs/SCOPE.md)** — the agreed plan: method, safeguards
  against self-deception, and what is deliberately out of scope.
- **[docs/FINDINGS.md](docs/FINDINGS.md)** — a running log of what the data
  actually turned out to be, including the bugs measurement caught.

## Method notes

**Ticker extraction is the highest-risk step**, because its errors are
systematic rather than random — reading "IT" as a ticker in a thousand comments
does not average away. Ambiguity is decided by word frequency: symbols as common
as *here* or *open* are never matched bare, ones as rare as *okta* or *palantir*
always are, and the awkward middle (*apple*, *ford*, *snow*) needs trading
context. Precision by method was measured by hand on real comments.

**Point-in-time discipline.** Every symbol carries its first trade date and
mentions before it are dropped — `RDDT` was being matched in March 2022 text,
two years before Reddit went public. Size buckets use dollar volume computed
from past days only, since bucketing by today's market cap would sort companies
by how big they eventually became.

**Survivorship is reported, not hidden.** Delisted companies are the ones Reddit
hyped most, and some cannot be priced at all (`SIVB`, `BBBYQ`). They are counted
as a headline number and treated as total losses rather than dropped, which
would flatter every result.

**Guarding against self-deception.** All data from 2024 onward is sealed and
touched exactly once, at the end. Every choice — training window, averaging
window, thresholds — is made without it.

## Data handling

Only comment text, timestamp, score and a pseudonymous author handle are stored.
The handle is used solely to count distinct participants, so one person posting
fifty times is not counted fifty times. No personal information is collected, no
public user profiles are produced, and no Reddit content or derived dataset is
redistributed. Deleted and removed content is dropped on refresh.
