# Reddit-Only Stock Selection — Project Scope

**Status:** agreed plan, pre-implementation
**Last updated:** 2026-09-01

## The goal

Find stocks *before* they rise, using nothing but what people say on Reddit.

The specific bet we are testing: somewhere in Reddit's noise there are people who
know something — through research, industry knowledge, or being early to a story —
and their posts show up *before* the price moves. Everything below is built to find
out whether that is true, and to avoid fooling ourselves if it isn't.

## The one question that gates everything

**Does Reddit chatter come before price moves, or after?**

People usually post about a stock *because* it jumped. If that is all that is
happening, the chatter is a rear-view mirror and there is no strategy here.

So the first real deliverable is not a trading system. It is one chart:

> Take every moment chatter about a stock spiked. Line all those moments up at
> "day zero". Average the price behaviour across thousands of them.

If prices rise *before* day zero, Reddit is late and the project stops or pivots.
If they rise *after*, we have something worth building on.

This is cheap to produce and could save the entire rest of the project. It runs
before any strategy code is written.

---

## Data

### Reddit history

| Source | Verdict |
|---|---|
| **Arctic Shift API** | ✅ Primary. Posts and comments, verified working back to 2017 |
| Pushshift | ❌ Shut down to the public |
| PullPush | ❌ Blocks automated access |
| Bulk dump files | ❌ No reachable file listing |
| Official Reddit API (PRAW) | Forward collection only — added later, needs credentials |

Verified constraints: page size caps at **100 records**, roughly **0.8s per request**.

This rules out downloading every WSB comment — that would run to hundreds of hours.
It shapes the two-track design below.

### Prices

Yahoo chart API. Confirmed available: daily open/high/low/close, **volume**,
adjusted close, and **first trade date**.

Confirmed *not* available: market cap, shares outstanding. See "Company size" below.

### Subreddits

`wallstreetbets`, `stocks`, `investing`, `StockMarket`, `options`, `pennystocks`,
`ValueInvesting`, `SecurityAnalysis`.

### Time range

2017 to present. Covers three presidential terms (Trump I, Biden, Trump II).

---

## Two tracks, run separately and compared

Both use identical scoring, so the comparison is fair.

### Track A — "What are your moves tomorrow"

WSB posts one such thread per trading day. We take those threads and all their comments.

Why this track is attractive:

- **Affordable.** Roughly 40 seconds of downloading per trading day — about half a
  day of unattended work for the full history. Verified: comments are fully
  retrievable per thread.
- **Intent is built in.** A comment in this thread is someone naming a stock they
  plan to buy, not a joke or a loss screenshot.
- **Honest timing.** The thread is posted the evening before. A comment in
  Thursday's thread is a genuine prediction about Friday. No accidental use of
  future information.

### Track B — Broad post capture

All submissions across all eight subreddits, full history. Titles and body text.
Comments only where volume permits.

### Why both

If Track A wins, intent matters more than volume. If Track B wins, the signal is
really just crowd attention. Either answer is informative, and the marginal cost of
running both is small since they share all downstream machinery.

---

## Turning text into signals

### Step 1 — Which stocks are being named

The hardest and highest-risk step. Real ticker symbols include `IT`, `ON`, `ALL`,
`A`, `OPEN`, `SO`, `NOW`, `EV`, `PM`. Naive matching produces garbage.

Approach: curated list of listed tickers, aggressive English-word blacklist,
company-name aliases ("Tesla" → TSLA), and an AI model to resolve ambiguous cases.

**This step ships with a measured accuracy number** from a hand-labelled sample.
Not a judgement call — a number. Everything downstream inherits these errors.

### Step 2 — Signals per stock per day

- **Unique authors** mentioning it — the primary volume measure. One person
  shouting fifty times counts once, which defeats brigading and pump groups.
- **Unusualness** — how far above that stock's own normal level today is, not its
  level relative to other stocks.
- **Sentiment** and its direction of change.
- **Disagreement** — how split opinion is.
- **Novelty** — first appearance, or reappearance after long silence.
- **Author track record weighting** — see below.
- **Reasoning quality weighting** — see below.

### Author track record

Score each Reddit user by how their past mentions actually performed, then weight
their mentions accordingly. A handful of consistently right posters is worth more
than thousands of noise accounts. This is the most direct attempt to capture
"people who actually know something."

> **Hard rule:** a user's score on any given day may use *only* their record up to
> that day. Crediting someone for calls they had not yet made would produce
> spectacular fake results. This is the single easiest place in the project to
> cheat by accident.

### Reasoning quality

Have an AI model judge whether a comment contains an actual argument — numbers, a
thesis, a catalyst — or is just "🚀🚀🚀". Weight accordingly. One researched post
carries more information than fifty emoji.

Real comments pulled from a live thread, as a reminder of the raw material:

> "Which one 😂" · "The Nvidia God" · "3m" · "Loss porn enthusiast"

---

## The screens

### Quiet buildup — the primary screen

The direct expression of the project's goal. Two conditions at once:

1. Chatter clearly above that stock's normal level, **and**
2. Price still flat over the same window

This deliberately excludes stocks that already ran — which, given the lead/lag
problem, is likely where most of the noise lives. "Most mentioned" is a blunt
instrument; "talked about but hasn't moved yet" is a sharp one.

### Skip what the market already knows

If chatter spikes because earnings just landed, there is no edge — the news is
already priced. Flag earnings dates; test with and without those windows. What we
want is chatter with *no obvious public trigger*.

---

## Company size buckets

Any edge is far likelier in small, less-covered companies. Mega caps have thousands
of professional analysts; Reddit will not beat them on NVDA.

**Bucketing uses average daily dollar volume** (price × volume), not market cap.

Why: market cap is unavailable from our price source, and the obvious workaround —
looking up each company's size *today* — is cheating. NVDA was mid-cap once.
Bucketing by eventual size means sorting stocks by how big they turned out to be.
Dollar volume is computable correctly at every past date, and it also tells us how
much money could realistically be deployed without moving the price.

| Bucket | Avg daily dollar volume |
|---|---|
| Mega | > $1B |
| Large | $200M – $1B |
| Mid | $50M – $200M |
| Small | $5M – $50M |
| Micro | < $5M |

Cross-cutting flags: **newly listed** (under 12 months, from first trade date) and
**penny** (under $5/share).

Optional later upgrade: true point-in-time market cap from SEC filings, which
publish share counts with filing dates.

---

## Survivorship bias — a known, partly unfixable problem

Verified: some delisted tickers are unavailable from our price source.
`FTCHQ` worked; **`SIVB` (Silicon Valley Bank) and `BBBYQ` (Bed Bath & Beyond) did not.**

These are exactly the stocks Reddit talked about most. They went to zero.

If we quietly skip stocks we cannot price, we delete the worst outcomes from the
record and the strategy looks dramatically better than it was. This is the classic
way backtests lie.

**Our handling:**

- Count every mentioned stock we cannot price and **report that count as a headline
  number**, not a footnote.
- Treat "mentioned, then delisted, then unpriceable" as a **total loss** rather than
  dropping it.

Slightly pessimistic. Wrong in the safe direction.

---

## Noise and signal

### Two kinds of noise, and only one of them averages away

- **Random noise** — people chatting about nothing. Shrinks with the square root of
  how many samples you average. More data genuinely fixes it.
- **Systematic error** — reading "IT" as a ticker, bots posting daily. **Averaging
  makes this worse.** You get a very precise wrong answer, and more data makes you
  more confident in it.

So the filtering work is not optional cleanup we can skip by gathering more data.
It is the only thing that addresses the second category.

### Choosing the averaging window

Averaging cuts random noise, but if the real effect lasts 3 days and we average over
20, we dilute it with 17 days of noise and destroy the thing we are hunting.

We do not guess. We test windows of **1, 3, 5, 10 and 21 days** and measure the
noise ratio at each. The shape of that curve tells us how long the effect actually
lasts.

Defined concretely:

> noise ratio = (average return gap between high-chatter and low-chatter stocks)
> ÷ (how much that gap bounces around)

Computed for every combination of window length and mention threshold. A table we
can read, not a judgement call.

**Honest limit:** we have roughly 2,000 trading days. Weekly gives ~400 data points;
monthly only ~96. Below that you cannot distinguish a real effect from luck.
Weekly is about as far as averaging can sensibly be pushed; 3–5 days is the likely
sweet spot.

### Boosting signal, as distinct from reducing noise

- **Event stacking** (see "The one question" above) — the technique used to pull
  faint brain signals out of EEG noise. Random noise in each event points a
  different way and cancels; what is common survives.
- **Do not dilute** — if the effect lives only in small caps, including mega caps
  adds noise and no signal. Size bucketing is itself a signal booster.
- **Weight by informativeness** — author track record and reasoning quality. Same
  principle as weighting measurements by instrument precision.
- **Combine only genuinely different signals** — mention count and comment count are
  the same number in different clothes. Chatter volume, author quality and sentiment
  direction are actually different.
- **Subtract what is already explainable** — remove the part of each move explained
  by the market, the sector, and recent trend. Only the unexplained remainder could
  possibly be Reddit information. Background subtraction.
- **Compare stocks against each other on the same day** rather than using absolute
  values, so market-wide moves cancel out.
- **Look where the background is quiet** — chatter about a normally-ignored stock
  carries far more information than more chatter about Tesla.

**The limit worth stating plainly:** processing can only recover signal that is
there. If Reddit chatter contains no advance information, none of this manufactures
any — it just makes us better at confirming the absence.

---

## Guarding against fooling ourselves

### Three-way data split

| Split | Period | Use |
|---|---|---|
| **Training** | 2017 – 2022 | Build on it freely |
| **Validation** | 2023 | Make choices on it, as often as we like |
| **Test** | **2024 onward** | **Touched exactly once, at the very end** |

The 2024+ data is sealed. Every choice — which training period, which window, which
threshold — is made without it.

### Which training period predicts best

Compared using rolling tests **inside pre-2024 data only**:

train ≤2020 → check 2021 · train ≤2021 → check 2022 · train ≤2022 → check 2023

Periods compared:

- 2017–2020 (before the meme era)
- 2021–2023 (after it)
- 2017–2023 (everything)
- Rolling recent — last 2 years only, refreshed annually

If "recent only" wins, Reddit's behaviour keeps changing and old data is actively
misleading. That is a useful finding in its own right, independent of any trading result.

The single winner then runs on 2024+ **once**. Whatever comes out is the answer,
including if it is bad.

> **Why this matters:** if we tried four training periods against 2024 and kept the
> winner, we would have used the 2024 data to make a choice. It is no longer an
> untouched exam — just another thing we tuned on.

### Regime slicing

Results reported separately for each presidential term and for calm vs volatile
market conditions.

**Caveat that must be applied:** WSB grew from a niche forum to millions of members
after early 2021. Raw mention counts across presidents would mostly measure *the
forum growing*. All cross-era comparisons therefore use relative measures — a
stock's *share* of the day's chatter, and how unusual today is versus its own recent
normal — which stay comparable as the forum grows.

### Benchmarks

The bar is not "beats the S&P 500". The bar is:

**Does it beat a mention-count-only baseline with no sentiment analysis at all?**

If sentiment adds nothing over simply counting mentions, that is the finding, and
the report will say so.

---

## Build order

1. **Download** — Track A threads and Track B posts, 2017→now
2. **Extract** — which stocks are named, with a measured accuracy number
3. **Lead/lag study** — the event-stacking chart. **This is the gate.**
4. **Signals and screens** — only if step 3 is promising
5. **Simulation** — buy/sell rules, costs, next-day execution, long-only top-N
6. **Report** — sliced by size bucket, presidential term, and market conditions
7. **Sealed test** — 2024+, once
8. **Forward collection** — live Reddit API, after backtesting is done, once
   credentials are provided

Steps 1–7 need no credentials and no live data.

## Explicitly out of scope for v1

- Live trading or broker integration
- Forward data collection (deferred until backtesting completes)
- Data sources other than Reddit — no news, no fundamentals, no technicals as inputs
  (market and sector returns are used only for subtraction, not as signals)

## Honest expectations

Published research on this kind of signal is mixed in a specific way. Effects tend
to be concentrated in small and mid-size companies with heavy retail ownership, to
decay within days, and to reflect *attention* rather than *opinion*. Extreme chatter
spikes often mean-revert rather than continue.

We may well find nothing. The plan above is designed so that finding nothing is a
clear, cheap, early result rather than an expensive ambiguous one.
