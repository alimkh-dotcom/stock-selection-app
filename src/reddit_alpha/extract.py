"""Finding which companies a piece of Reddit text is talking about.

This is the highest-risk step in the project. Everything downstream inherits its
errors, and those errors are systematic rather than random: reading "IT" as
Information Services Group in a thousand comments does not average away, it
produces a confidently wrong answer that more data only reinforces.

Three routes to a symbol, in descending order of confidence:

``cashtag``  ``$GME``. Unambiguous by construction, and accepted even when the
             symbol is not in the current universe so that companies which have
             since delisted are still found.
``context``  A bare uppercase symbol that is also an ordinary English word, but
             sits near trading vocabulary: "ALL calls printed". Recovers real
             mentions the blacklist would otherwise discard. Words that are
             pervasive in trading prose ("IT", "DD", "BUY") are excluded even
             here -- context is meaningless when every comment contains it.
``bare``     A bare uppercase symbol that is not an English word. ``NVDA`` is not
             a word, so an uppercase match is safe.

Company names are matched separately, since "Tesla" carries the same information
as "TSLA" and appears at least as often.

Every match records how it was found, so any analysis can be re-run at a chosen
confidence level and the effect measured rather than assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .universe import CONTEXT_TERMS, Universe

# Cashtags: $ followed by 1-5 letters, optionally a class suffix ($BRK.B).
CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})(?:\.([A-Za-z]))?\b")

# Bare candidates: uppercase runs of 1-5 characters. Case-sensitive on purpose --
# "it" is a word, "IT" might be a ticker, and lowercasing first would destroy the
# only cheap signal separating them.
BARE_RE = re.compile(r"\b([A-Z]{1,5})\b")

# How far either side of a candidate to look for trading vocabulary.
CONTEXT_WINDOW = 6

# Very short company names ("Now", "Key") collide with ordinary words the same
# way short tickers do, so name matching ignores them.
MIN_NAME_LENGTH = 4


@dataclass(frozen=True)
class Mention:
    symbol: str
    method: str          # cashtag | bare | context | name
    confidence: float

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.8


def _tokens_around(tokens: list[str], index: int, window: int) -> list[str]:
    lo = max(0, index - window)
    return tokens[lo : index + window + 1]


def _has_trading_context(lower_tokens: list[str], index: int) -> bool:
    return any(t in CONTEXT_TERMS for t in _tokens_around(lower_tokens, index, CONTEXT_WINDOW))


class TickerExtractor:
    def __init__(self, universe: Universe, include_etfs: bool = False) -> None:
        self.universe = universe
        self.include_etfs = include_etfs
        self._name_index = self._build_name_index(universe)
        # Cashtags naming something outside the current universe are usually
        # delisted companies -- the population most at risk of vanishing from
        # this study -- so they are counted rather than silently dropped.
        self.unknown_cashtags: dict[str, int] = {}

    @staticmethod
    def _build_name_index(universe: Universe) -> dict[str, str]:
        index: dict[str, str] = {}
        for symbol, name in universe.names.items():
            if len(name) >= MIN_NAME_LENGTH and " " not in name:
                # Ambiguous single words ("block", "square") are still risky, but
                # multi-word names rarely appear verbatim in casual writing.
                index.setdefault(name, symbol)
        return index

    def _admissible(self, symbol: str) -> bool:
        if not self.include_etfs and symbol in self.universe.etfs:
            return False
        return True

    def extract(self, text: str) -> list[Mention]:
        """Return the distinct symbols ``text`` mentions.

        One mention per symbol per text: a comment repeating "GME" ten times is
        one person's opinion, not ten.
        """
        if not text:
            return []

        found: dict[str, Mention] = {}

        for match in CASHTAG_RE.finditer(text):
            symbol = match.group(1).upper()
            if match.group(2):
                symbol = f"{symbol}.{match.group(2).upper()}"
            if symbol not in self.universe:
                self.unknown_cashtags[symbol] = self.unknown_cashtags.get(symbol, 0) + 1
            if self._admissible(symbol):
                self._offer(found, Mention(symbol, "cashtag", 0.95))

        tokens = text.split()
        lower_tokens = [t.lower().strip(".,!?:;()[]\"'") for t in tokens]

        for i, token in enumerate(tokens):
            cleaned = token.strip(".,!?:;()[]\"'")
            if not BARE_RE.fullmatch(cleaned):
                continue
            if cleaned not in self.universe or not self._admissible(cleaned):
                continue
            if self.universe.needs_cashtag(cleaned):
                # Too common in ordinary trading prose for context to mean
                # anything -- only an explicit cashtag counts for these.
                continue
            if self.universe.is_ambiguous(cleaned):
                if _has_trading_context(lower_tokens, i):
                    self._offer(found, Mention(cleaned, "context", 0.6))
                continue
            self._offer(found, Mention(cleaned, "bare", 0.85))

        token_set = set(lower_tokens)
        for name, symbol in self._name_index.items():
            if name not in token_set or not self._admissible(symbol):
                continue
            if name in self.universe.context_names:
                # Measured on real comments, context-rescued name matches were
                # right 0 times out of 4: "apple pie patriotism", "selling on
                # eBay", "decent action shares". Trading vocabulary is too dense
                # here to vouch for an everyday word, exactly as it was for bare
                # symbols like DD. These names are matched only via their ticker.
                continue
            self._offer(found, Mention(symbol, "name", 0.8))

        return sorted(found.values(), key=lambda m: m.symbol)

    @staticmethod
    def _offer(found: dict[str, Mention], mention: Mention) -> None:
        """Keep the most confident route by which a symbol was found."""
        existing = found.get(mention.symbol)
        if existing is None or mention.confidence > existing.confidence:
            found[mention.symbol] = mention
