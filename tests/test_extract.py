"""Extraction tests.

The hostile cases matter more than the happy ones: this step's errors are
systematic, so a false positive that fires on every comment containing "it"
would corrupt the whole study while looking like a working pipeline.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.extract import TickerExtractor
from reddit_alpha.universe import Universe


@pytest.fixture
def extractor():
    universe = Universe(
        symbols=frozenset({"GME", "TSLA", "NVDA", "IT", "ALL", "ON", "OPEN", "DD",
                           "A", "SPY", "BRK.B", "PLTR", "F"}),
        names={"TSLA": "tesla", "NVDA": "nvidia", "GME": "gamestop",
               "PLTR": "palantir", "F": "ford"},
        etfs=frozenset({"SPY"}),
    )
    return TickerExtractor(universe)


# --- the collision problem -------------------------------------------------

@pytest.mark.parametrize("text", [
    "I think it is going up",
    "buy it now",
    "all of my money is gone",
    "I put all in on this",
    "sitting on a huge loss",
    "did my DD before buying",           # DD is DuPont, but means due diligence
    "a friend told me about this",
])
def test_english_words_are_not_read_as_tickers(extractor, text):
    """These words are all real tickers. Matching them would swamp the data."""
    assert extractor.extract(text) == []


def test_uppercase_english_word_alone_is_still_rejected(extractor):
    """WSB shouts; 'BUY IT NOW' must not become a position in Information Services."""
    assert [m.symbol for m in extractor.extract("BUY IT NOW")] == []


# --- recovering the real mentions ------------------------------------------

def test_cashtag_is_unambiguous(extractor):
    mentions = extractor.extract("loading up on $GME and $TSLA")
    assert [m.symbol for m in mentions] == ["GME", "TSLA"]
    assert all(m.method == "cashtag" for m in mentions)


def test_unambiguous_bare_symbol_is_found(extractor):
    (m,) = extractor.extract("NVDA earnings tomorrow")
    assert m.symbol == "NVDA" and m.method == "bare"


def test_ambiguous_symbol_recovered_by_trading_context(extractor):
    """'ALL calls' is a position; 'all of it' is not. Context separates them."""
    (m,) = extractor.extract("my ALL calls printed today")
    assert m.symbol == "ALL" and m.method == "context"
    assert m.confidence < 0.8, "context matches must not claim high confidence"


def test_pervasive_abbreviation_is_never_rescued_by_context(extractor):
    """Trading words are everywhere here, so context cannot vouch for 'DD' or 'IT'."""
    assert extractor.extract("did my DD before buying calls") == []
    assert extractor.extract("BUY IT NOW before earnings") == []


def test_pervasive_abbreviation_still_works_as_a_cashtag(extractor):
    """An explicit cashtag is unambiguous even for the worst offenders."""
    assert [m.symbol for m in extractor.extract("$DD is a chemicals play")] == ["DD"]


def test_company_names_are_matched(extractor):
    assert [m.symbol for m in extractor.extract("tesla is overvalued")] == ["TSLA"]


def test_class_share_cashtag(extractor):
    assert [m.symbol for m in extractor.extract("$BRK.B is a fortress")] == ["BRK.B"]


# --- counting discipline ---------------------------------------------------

def test_repeated_symbol_counts_once(extractor):
    """One comment is one opinion, however many times it shouts the ticker."""
    assert len(extractor.extract("GME GME GME $GME gamestop to the moon")) == 1


def test_best_method_wins_when_several_match(extractor):
    (m,) = extractor.extract("$TSLA tesla calls")
    assert m.method == "cashtag", "a weaker route overwrote a stronger one"


# --- survivorship ----------------------------------------------------------

def test_delisted_cashtag_is_kept_and_counted(extractor):
    """Delisted names are exactly the ones Reddit hyped. Dropping them flatters results."""
    mentions = extractor.extract("still holding $BBBY bags")
    assert [m.symbol for m in mentions] == ["BBBY"]
    assert extractor.unknown_cashtags["BBBY"] == 1


# --- misc ------------------------------------------------------------------

def test_etfs_excluded_by_default(extractor):
    """SPY dominates chatter and is not a stock pick."""
    assert extractor.extract("$SPY puts") == []


def test_etfs_included_when_asked():
    universe = Universe(symbols=frozenset({"SPY"}), names={}, etfs=frozenset({"SPY"}))
    assert [m.symbol for m in TickerExtractor(universe, include_etfs=True).extract("$SPY puts")] == ["SPY"]


def test_punctuation_does_not_hide_a_symbol(extractor):
    assert [m.symbol for m in extractor.extract("(NVDA), PLTR!")] == ["NVDA", "PLTR"]


def test_empty_and_junk_text(extractor):
    assert extractor.extract("") == []
    assert extractor.extract("🚀🚀🚀") == []
    assert extractor.extract("3m") == []


def test_lowercase_cashtag_is_normalised(extractor):
    assert [m.symbol for m in extractor.extract("$gme")] == ["GME"]
