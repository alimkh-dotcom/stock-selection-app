"""The set of tradeable symbols, and which of them are dangerous to match.

Two hazards shape this module.

**English collisions.** Roughly 70 of the 180 commonest English words are also
live tickers -- ``IT``, ``ON``, ``ALL``, ``OPEN``, ``NOW``, ``SO``. Matching bare
uppercase words against the ticker list without guarding these produces a corpus
that is mostly noise, and no amount of later averaging repairs it: it is
systematic error, not random error, so more data only makes the wrong answer
more confident.

**Survivorship in the symbol list itself.** These files list what trades *today*.
A company that listed in 2018 and died in 2022 is absent, so its mentions would
be invisible -- and those are exactly the companies Reddit talked about most.
Cashtags (``$BBBY``) are therefore accepted whether or not the symbol is still
listed, which recovers the delisted names at the cost of admitting some
nonsense. The count of cashtags outside the universe is tracked so the size of
that trade-off stays visible.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests
from wordfreq import zipf_frequency

log = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# Ambiguity comes in two strengths, and conflating them was a real bug: trading
# vocabulary is so dense on these subreddits that "context" rescues almost
# anything. "did my DD before buying" is due diligence next to the word "buying",
# not a position in DuPont; "BUY IT NOW" is not a position in Information
# Services. Words this pervasive can never be recovered by context, only by an
# explicit cashtag.
CASHTAG_ONLY_SYMBOLS = frozenset("""
A AN AND ANY ARE AS AT BE BUT BY CAN FOR GO HAS HE IF IN IS IT ME MY NO NOT NOW
OF ON OR OUT SO THE TO TWO UP US WE YOU ONE BIG NEW OLD YES OK OKAY
DD CEO CFO IPO ATH ATL EPS PE PT YOLO FD FDS OTM ITM ATM EOD EOW EOY PSA EDIT
TLDR IMO IMHO AF LOL WTF LMAO GG RIP TA FA ER CC PM AM EST PST UTC GMT
USA USD EUR GDP CPI FED SEC IRS FBI CIA NYSE ETF ETN REIT LLC INC CORP LTD
Q1 Q2 Q3 Q4 FY YTD YOY QOQ MOM API APR APY EV IV OI VIX
HODL FOMO FUD MOON APE APES BAG BAGS CALL CALLS PUT PUTS BUY SELL HOLD LONG SHORT
I II III IV V X XX XXX WSB GAIN GAINS LOSS LOSSES
""".split())

# These are ordinary words too, but not so common that trading context is
# meaningless: "my ALL calls printed" really is Allstate. Accepted only with
# nearby trading vocabulary, and at reduced confidence.
CONTEXT_REQUIRED_SYMBOLS = frozenset("""
ALL CASH DARE DNA EARN FAST GOLD GOOD HOPE KEY LAND LIFE LINK LIVE LOVE LOW MIND
MOVE NEXT NICE ODD OPEN PAY PLAY PLUS POST PURE RACE REAL ROAD ROCK RUN SAFE SHIP
SITE SKIN SNAP SOS SPOT STEP SUN TEAM TECH TOP TOUR TREE UNIT WAVE WAY WELL WEST
ZONE
""".split())

# Every symbol needing more than a bare uppercase match.
AMBIGUOUS_SYMBOLS = CASHTAG_ONLY_SYMBOLS | CONTEXT_REQUIRED_SYMBOLS

# Hand-listing English words does not scale -- running the extractor over real
# comments turned up "HERE", "SNOW", "COST" and others the list had missed, and
# there was no reason to think the next batch would be any kinder. Word
# frequency decides the tier instead, on the Zipf scale (log10 occurrences per
# billion words): 6 is "the", 3 is a word you meet in most books, 1 is rare.
#
# The thresholds are set from observed separation: "here" 5.97, "nice" 5.37 and
# "open" 5.48 are hopeless as bare tickers, while "okta" 1.56, "palantir" 2.18
# and "gamestop" 2.91 are unmistakable. In between sit the genuinely awkward
# ones -- "apple" 4.76, "ford" 4.50, "snow" 4.66 -- which are both real
# companies and real words, and which only trading context can settle.
CASHTAG_ONLY_ZIPF = 5.0
CONTEXT_REQUIRED_ZIPF = 3.8

# Finance vocabulary that general-English frequency underrates. "bullish" scores
# 3.14 -- rarer than "tesla" at 3.77 -- so frequency alone would happily read it
# as the ticker BLSH, which it did, 14 times in the first 3,000 comments.
DOMAIN_STOPWORDS = frozenset("""
bullish bearish calls puts call put strike expiry moon tendies stonks stonk
squeeze short long hold holding bag bags gain gains loss losses yolo hype
dip rip pump dump rally crash bounce breakout retrace hedge margin leverage
buy sell trade trading trader position size open close high low volume float
crypto bitcoin reddit webull robinhood vanguard fidelity schwab discord twitter
youtube google apple amazon market markets economy news earnings company stock
""".split())


def build_corpus_stopwords(texts: Iterable[str], top_n: int = 400) -> frozenset[str]:
    """Words common in *this* corpus, whatever general English says.

    General-English frequency misses domain vocabulary: "crypto", "reddit" and
    "webull" are unremarkable words on an investing forum but rare enough in
    books that frequency alone waves them through -- and each was matched as a
    company in the first real sample. Counting words in the corpus itself
    catches that class of error without anyone having to anticipate it.

    Note this deliberately also demotes heavily-discussed companies whose names
    are ordinary words. That is the correct trade: their tickers are mentioned
    constantly and carry the same information without the ambiguity.
    """
    import collections
    import re as _re

    counts: collections.Counter[str] = collections.Counter()
    for text in texts:
        counts.update(_re.findall(r"[a-z]{3,}", (text or "").lower()))
    return frozenset(word for word, _ in counts.most_common(top_n))


def english_frequency(word: str) -> float:
    """Zipf frequency of ``word`` in general English; 0.0 if unknown."""
    return zipf_frequency(word.lower(), "en")


def classify_symbol_ambiguity(symbol: str) -> str:
    """Return 'cashtag_only', 'context_required' or 'safe' for a bare match."""
    if symbol in CASHTAG_ONLY_SYMBOLS:
        return "cashtag_only"
    if symbol in CONTEXT_REQUIRED_SYMBOLS:
        return "context_required"
    lowered = symbol.lower()
    if lowered in DOMAIN_STOPWORDS:
        return "cashtag_only"
    freq = english_frequency(symbol)
    if freq >= CASHTAG_ONLY_ZIPF:
        return "cashtag_only"
    if freq >= CONTEXT_REQUIRED_ZIPF:
        return "context_required"
    return "safe"


def name_is_matchable(name: str) -> tuple[bool, bool]:
    """Whether a company name may be matched, and whether it needs context.

    Returns ``(matchable, needs_context)``. Names that are ordinary words are
    rejected outright or demoted to context-only by the same thresholds as
    symbols, which is what stops "nice", "here" and "legend" from being read as
    companies.
    """
    if name in DOMAIN_STOPWORDS:
        return False, False
    freq = english_frequency(name)
    if freq >= CASHTAG_ONLY_ZIPF:
        return False, False
    if freq >= CONTEXT_REQUIRED_ZIPF:
        return True, True
    return True, False

# Trading vocabulary near a symbol makes the ticker reading far more likely than
# the English one: "I bought ALL calls" versus "I bought all of them".
CONTEXT_TERMS = frozenset("""
call calls put puts share shares stock stocks ticker position positions
buy buying bought sell selling sold long short hold holding bag bags
strike expiry expiration leap leaps option options contract contracts
earnings squeeze dip rip moon bullish bearish yolo dd float shorted
premarket afterhours ipo split dividend div ath rsi macd support resistance
""".split())


@dataclass
class Universe:
    symbols: frozenset[str]
    names: dict[str, str]
    etfs: frozenset[str] = frozenset()
    ambiguous: frozenset[str] = AMBIGUOUS_SYMBOLS
    cashtag_only: frozenset[str] = CASHTAG_ONLY_SYMBOLS
    context_names: frozenset[str] = frozenset()
    _alias_index: dict[str, str] = field(default_factory=dict, repr=False)

    def is_ambiguous(self, symbol: str) -> bool:
        return symbol in self.ambiguous

    def needs_cashtag(self, symbol: str) -> bool:
        """True if no amount of surrounding context can justify a bare match."""
        return symbol in self.cashtag_only

    def __contains__(self, symbol: str) -> bool:
        return symbol in self.symbols

    def __len__(self) -> int:
        return len(self.symbols)


def _clean_company_name(name: str) -> str:
    """Strip the legal and instrument boilerplate from a listed security name.

    'Apple Inc. Common Stock' -> 'apple'. Without this, name matching keys on
    words like 'common' and 'stock' that appear in every other sentence.
    """
    lowered = name.lower()
    for cut in (
        " common stock", " class a", " class b", " class c", " ordinary shares",
        " american depositary", " depositary shares", " warrant", " units",
        " preferred stock", " - ", " etf", " trust", " fund",
    ):
        idx = lowered.find(cut)
        if idx > 0:
            lowered = lowered[:idx]
    for suffix in (" inc.", " inc", " corp.", " corp", " corporation", " company",
                   " co.", " co", " ltd.", " ltd", " plc", " l.p.", " lp",
                   " holdings", " holding", " group", " international", " n.v.",
                   " s.a.", " ag", " se", ","):
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
    return lowered.strip(" .,")


def build_universe(cache_dir: Path, refresh: bool = False) -> Universe:
    """Load the symbol universe, downloading and caching the source files."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    sec = _cached_json(cache_dir / "sec_tickers.json", SEC_TICKERS_URL, refresh)
    nasdaq = _cached_text(cache_dir / "nasdaqlisted.txt", NASDAQ_LISTED_URL, refresh)
    other = _cached_text(cache_dir / "otherlisted.txt", OTHER_LISTED_URL, refresh)

    names: dict[str, str] = {}
    symbols: set[str] = set()
    etfs: set[str] = set()

    for entry in sec.values():
        symbol = str(entry["ticker"]).strip().upper()
        if symbol:
            symbols.add(symbol)
            names.setdefault(symbol, _clean_company_name(str(entry["title"])))

    for text, sym_col, name_col, etf_col in (
        (nasdaq, "Symbol", "Security Name", "ETF"),
        (other, "ACT Symbol", "Security Name", "ETF"),
    ):
        for row in csv.DictReader(text.splitlines(), delimiter="|"):
            symbol = (row.get(sym_col) or "").strip().upper()
            # The files end with a "File Creation Time" trailer row.
            if not symbol or "file creation" in symbol.lower():
                continue
            if (row.get("Test Issue") or "").strip() == "Y":
                continue
            symbols.add(symbol)
            names.setdefault(symbol, _clean_company_name(row.get(name_col) or ""))
            if (row.get(etf_col) or "").strip() == "Y":
                etfs.add(symbol)

    cashtag_only: set[str] = set(CASHTAG_ONLY_SYMBOLS)
    context_required: set[str] = set(CONTEXT_REQUIRED_SYMBOLS)
    for symbol in symbols:
        tier = classify_symbol_ambiguity(symbol)
        if tier == "cashtag_only":
            cashtag_only.add(symbol)
        elif tier == "context_required":
            context_required.add(symbol)

    usable_names: dict[str, str] = {}
    context_names: set[str] = set()
    for symbol, name in names.items():
        if not name:
            continue
        matchable, needs_context = name_is_matchable(name)
        if not matchable:
            continue
        usable_names[symbol] = name
        if needs_context:
            context_names.add(name)

    log.info(
        "Universe: %d symbols (%d cashtag-only, %d context-required), "
        "%d ETFs, %d usable names (%d need context)",
        len(symbols), len(cashtag_only), len(context_required),
        len(etfs), len(usable_names), len(context_names),
    )
    return Universe(
        symbols=frozenset(symbols),
        names=usable_names,
        etfs=frozenset(etfs),
        ambiguous=frozenset(cashtag_only | context_required),
        cashtag_only=frozenset(cashtag_only),
        context_names=frozenset(context_names),
    )


def _cached_json(path: Path, url: str, refresh: bool) -> dict[str, Any]:
    return json.loads(_cached_text(path, url, refresh))


def _cached_text(path: Path, url: str, refresh: bool) -> str:
    if path.exists() and not refresh:
        return path.read_text()
    log.info("Downloading %s", url)
    # The SEC asks automated clients to identify themselves.
    resp = requests.get(
        url, timeout=60, headers={"User-Agent": "reddit-alpha-research (contact via repo)"}
    )
    resp.raise_for_status()
    path.write_text(resp.text)
    return resp.text
