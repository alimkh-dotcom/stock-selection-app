# Findings log

Running notes on what the data actually turned out to be. Recorded as we go, so
that later results can be read against the limits of what fed them.

---

## Data source throughput (measured, not estimated)

| Measurement | Value |
|---|---|
| Page size cap | 100 records/request |
| Comment read rate | ~70 comments/sec sustained |
| Post crawl | ~10.2s per subreddit-day (wallstreetbets, ~564 posts/day) |
| Daily thread size | >1,000 comments; uncapped reads exceeded 9.5 min/thread |
| Record size | 3,333 bytes raw, 315 compressed, **108 after pruning** |

`after` is **exclusive** on `created_utc`, so naive paging drops every record
sharing a second with the last one on a page. Pages overlap by one second and
de-duplicate by id. A second holding more than one page is read from both ends
(ascending reaches the first 100, descending the last 100); beyond 200 in one
second the API exposes no cursor and the gap is logged as real data loss.

### Rate limiting is the binding constraint

The archive is a free, shared service and throttles hard under sustained load.
After the pilot crawls it began rejecting most requests with HTTP 422
`"Timeout. Maybe slow down a bit"`, and did not recover at 20s spacing. Fast
failures (0.8s) mark rate-limit rejection rather than genuine query timeouts.

**Consequence: the full crawl in SCOPE.md is not achievable against this
endpoint from this environment.** Track A alone needs roughly 23,000 requests;
at the throttled rate that is measured in weeks, not hours.

Thread discovery was reworked from a nine-hour full post scan to a title search
costing minutes, which helps but does not close the gap — the comments are the
expensive part and there is no cheaper route to them.

---

## Daily thread naming changed repeatedly

Discovery must match on title, not author. The posting account changed
(AutoModerator through 2021, `OPINION_IS_UNPOPULAR` by 2023) and the title
format changed at least four times:

| Era | Title |
|---|---|
| 2017 | `[Discussion] What Are Your Moves Tomorrow, March 02` |
| 2019 | `What Are Your Moves Tomorrow, March 04` + `Daily Discussion Thread` |
| 2021 | `Unpinned Daily Discussion Thread for March 04 2021` |
| 2023 | `Daily Discussion Thread for March 06, 2023` |
| 2025 | `What Are Your Moves Tomorrow, March 03, 2025` |

Matching on author would have silently lost whole years. Each observed title is
pinned as a regression test. Archive coverage itself was checked and is intact
across all these years — an apparent 2023 gap was the author change, not missing
data.

---

## Ticker extraction: measured, then fixed

Hand-written blacklists did not survive contact with real comments. Running the
first version over 3,000 collected comments surfaced failures the list had not
anticipated — `HERE` ("HERE IS WHY"), `SNOW`, `COST`, and worse, company-name
matches firing on `BLSH` for the word "bullish" (14 times), `NICE` for "nice",
`PPLI`, `FPF`, `HAPN` for ordinary prose.

Ambiguity is now decided by **word frequency** rather than by hand, on the Zipf
scale (6 ≈ "the", 3 ≈ an ordinary book word, 1 ≈ rare):

| Tier | Threshold | Behaviour | Examples |
|---|---|---|---|
| Cashtag only | zipf ≥ 5.0 | never matched bare | `here` 5.97, `nice` 5.37, `open` 5.48 |
| Context required | 3.8 ≤ zipf < 5.0 | needs trading vocabulary nearby | `apple` 4.76, `ford` 4.50, `snow` 4.66 |
| Safe | zipf < 3.8 | matched bare | `okta` 1.56, `palantir` 2.18, `gamestop` 2.91 |

Frequency alone is not sufficient: **"bullish" scores 3.14, rarer than "tesla"
at 3.77**, so general English would wave it through as the ticker BLSH. Domain
vocabulary is therefore excluded separately, and a corpus-frequency stoplist
catches this class of error without anyone having to predict it in advance.

### Precision by method (45 hand-judged mentions)

| Method | Correct | Verdict |
|---|---|---|
| `cashtag` | 3/3 | reliable |
| `bare` | 23/24 (96%) | reliable |
| `context` | 6/6 | reliable |
| `name` | 4/7 (57%) | weak — failures were `webull`, `reddit`, `crypto` |
| `name_context` | **0/4** | **removed** |

Overall precision was ~80% before these fixes; after removing `name_context` and
the domain-word name matches, roughly 88% on the same sample.

The `name_context` result repeats the lesson that broke `DD` earlier: on an
investing forum, trading vocabulary sits near *everything*, so it cannot vouch
for an everyday word. "apple pie patriotism", "selling on eBay" and "decent
action shares" were all matched as companies.

> This sample is 45 mentions from three threads in one week. It is enough to
> have caught several real defects, and not enough to be a precision estimate
> for the corpus. A larger stratified sample is still owed.

### Two point-in-time hazards

**Delisted companies are missing from the symbol list.** It lists what trades
today, so a company that listed in 2018 and died in 2022 is invisible — and
those are exactly the names Reddit hyped. Cashtags are therefore accepted even
for unknown symbols, and the count of such cashtags is tracked (`$BBBY`, `$JWN`,
`$WISH`, `$VSCO` already appeared in the pilot).

**Companies that had not yet listed are wrongly matchable.** `RDDT` was matched
in March 2022 text; Reddit did not go public until 2024. The universe must be
gated by first trade date, which the price source does supply. Not yet
implemented.

---

## Price data

Available: daily OHLC, **volume**, adjusted close, and **first trade date**.
Not available: market cap or shares outstanding — hence dollar-volume bucketing.

**Delisted tickers are only partly retrievable.** `FTCHQ` returned 251 bars;
`SIVB` (Silicon Valley Bank) and `BBBYQ` (Bed Bath & Beyond) returned nothing.
Those are precisely the names this study most needs to price. Unpriceable
mentions are counted as a headline number and treated as total losses.

---

## Price layer and the lead/lag gate

### Transient failures must not be read as delistings

An integration run marked **SOFI unpriceable** after one malformed response.
SOFI is very much alive — it returns 103 bars on retry. Had that stood, a living
company would have been recorded as a delisting, inflating the survivorship
number that is a headline result of the study.

Outcomes are now separated: `no_data` means the provider answered and has
nothing (a real delisting, cached so thousands of dead symbols are not
re-requested every run), while `error` means the request failed and is retried,
never cached. Verified after the fix — SOFI resolves, and the genuinely
unpriceable set is `SIVB`, `BBBYQ` and `RDDT`, all correct: two delistings and
one company that had not yet listed in the requested window.

### The event study is validated against planted signals

An event study that cannot recover a signal it was handed says nothing when
pointed at real data — a null result would be indistinguishable from a bug. The
machinery is therefore tested on synthetic events with a known answer:

| Planted | Recovered |
|---|---|
| Jump 3 days **after** the spike | "chatter leads price" |
| Jump 3 days **before** the spike | "chatter follows price" |
| No jump at all | no drift either side |

Two corrections are built in rather than left to the caller:

**Market adjustment.** Each stock's return has the market's subtracted. Without
it, any day the whole market rallies makes every stack look bullish.

**Clustered standard errors.** Attention spikes are not independent — hundreds
of stocks spike together on frantic days and their returns move together.
Treating each as an independent observation would shrink the error bars and
manufacture significance out of a single exciting week. Events are averaged
within a calendar date first, and the spread taken across dates.

### Point-in-time gating is now enforced

Every symbol carries its first trade date, and mentions before it are dropped.
This closes the `RDDT` anachronism: matched in March 2022 text, two years before
Reddit went public.
