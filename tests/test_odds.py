"""Odds ingestion: the shape of a market decides whether we may de-vig it at all.

`SportEvent` carries exactly two outcomes. Handing it a three-way sport by reading
`home_team`/`away_team` and discarding the Draw is silent data loss - the two survivors
sum to less than 1, and de-vigging then inflates them toward the favourite. These tests
pin the two places that must refuse: the parser (cause) and the consensus (backstop).
"""

from predictionedge.odds import (
    BookOdds,
    SportEvent,
    _parse_odds_event,
    consensus_fair_all,
    consensus_fair_prob,
)


def _ev(bookmakers, home="Home FC", away="Away FC"):
    return {"id": "e1", "home_team": home, "away_team": away,
            "commence_time": "2026-08-20T18:00:00Z", "bookmakers": bookmakers}


def _book(key, outcomes):
    return {"key": key, "markets": [{"key": "h2h", "outcomes": [
        {"name": n, "price": p} for n, p in outcomes]}]}


def test_two_way_book_is_parsed():
    ev = _parse_odds_event(_ev([_book("pinnacle", [("Home FC", 1.80), ("Away FC", 2.10)])]))
    assert ev is not None
    assert ev.books[0].outcomes == (1.80, 2.10)


def test_three_way_book_is_dropped():
    """The Draw cannot be carried, so the book is skipped rather than truncated."""
    ev = _parse_odds_event(_ev([_book("pinnacle", [
        ("Home FC", 1.16), ("Draw", 6.60), ("Away FC", 15.42)])]))
    assert ev is None                 # no usable book left, so no event


def test_a_two_way_book_survives_alongside_a_three_way_one():
    ev = _parse_odds_event(_ev([
        _book("soft", [("Home FC", 1.16), ("Draw", 6.60), ("Away FC", 15.42)]),
        _book("pinnacle", [("Home FC", 1.80), ("Away FC", 2.10)]),
    ]))
    assert ev is not None
    assert [b.book for b in ev.books] == ["pinnacle"]


def test_consensus_drops_a_book_it_cannot_devig():
    """Backstop: even if a bad pair reaches here, it must not move the consensus."""
    good = BookOdds("pinnacle", (1.80, 2.10))
    # Legs summing under 1 - only possible if something was dropped upstream.
    bad = BookOdds("broken", (1.16, 15.42))
    both = consensus_fair_prob(SportEvent("e", "l", ("A", "B"), [good, bad]))
    only_good = consensus_fair_prob(SportEvent("e", "l", ("A", "B"), [good]))
    assert both == only_good


def test_consensus_is_none_when_no_book_survives():
    """No fair value means no edge. An empty result must never read as a clean one."""
    bad = BookOdds("broken", (1.16, 15.42))
    assert consensus_fair_prob(SportEvent("e", "l", ("A", "B"), [bad])) is None


# --- consensus_fair_all: the A/B instrumentation ----------------------------

def test_consensus_fair_all_matches_per_method_consensus():
    """Each key must equal the single-method consensus - one source of truth."""
    ev = SportEvent("e", "l", ("A", "B"),
                    [BookOdds("pinnacle", (1.80, 2.10)),
                     BookOdds("draftkings", (1.83, 2.05))])
    out = consensus_fair_all(ev)
    assert set(out) == {"fair_mult", "fair_power", "fair_shin"}
    assert abs(out["fair_mult"] - consensus_fair_prob(ev, "multiplicative")) < 1e-12
    assert abs(out["fair_power"] - consensus_fair_prob(ev, "power")) < 1e-12
    assert abs(out["fair_shin"] - consensus_fair_prob(ev, "shin")) < 1e-12


def test_consensus_fair_all_drops_a_bad_book_from_every_method():
    """A book one method can't de-vig is dropped from all three, so the A/B
    comparison always scores the same books."""
    good = BookOdds("pinnacle", (1.80, 2.10))
    bad = BookOdds("broken", (1.16, 15.42))
    assert consensus_fair_all(SportEvent("e", "l", ("A", "B"), [good, bad])) == \
        consensus_fair_all(SportEvent("e", "l", ("A", "B"), [good]))


def test_consensus_fair_all_none_when_no_book_survives():
    bad = BookOdds("broken", (1.16, 15.42))
    assert consensus_fair_all(SportEvent("e", "l", ("A", "B"), [bad])) is None
