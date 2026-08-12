"""Guards built from real false matches observed against live venues 2026-08-05.

Every "should be rejected" case below is a pair the naive matcher scored 0.86-1.00
and would have shipped as a 25-48c edge that does not exist.
"""

from predictionedge.crossvenue import (
    VenueMarket,
    fetch_kalshi,
    find_divergences,
    question_conflict,
    similarity,
    tokens,
)


def _k(title, price=0.50, end="2026-12-31T00:00:00Z"):
    return VenueMarket("kalshi", "K1", title, price, end, 1000.0, "")


def _p(title, price=0.50, end="2026-12-31T00:00:00Z"):
    return VenueMarket("polymarket", "0xP1", title, price, end, 50000.0, "")


def test_tokens_drop_stopwords():
    assert tokens("Will the Fed cut rates?") == {"fed", "cut", "rates"}


def test_similarity_rewards_containment():
    # Kalshi titles are terse, Polymarket's verbose; one containing the other scores 1.
    assert similarity("Fed cuts rates", "Will the Fed cuts rates in March?") == 1.0
    assert similarity("Fed cuts rates", "Yankees win the World Series") == 0.0


# --- the real false matches -------------------------------------------------

def test_run_for_vs_win_is_a_conflict():
    k = "Who will run for the Democratic presidential nomination Alexandria Ocasio-Cortez"
    p = "Will Alexandria Ocasio-Cortez win the 2028 Democratic presidential primary?"
    assert similarity(k, p) > 0.7          # naive matcher rated it a strong match
    assert question_conflict(k, p)         # and it is wrong


def test_nominee_vs_winning_the_election_is_a_conflict():
    k = "2028 Republican presidential nominee Marco Rubio"
    p = "Will Marco Rubio win the 2028 US Presidential Election?"
    assert question_conflict(k, p)


def test_identical_question_is_not_a_conflict():
    k = "Will Marco Rubio win the 2028 Republican nomination"
    p = "Will Marco Rubio win the 2028 Republican nomination?"
    assert not question_conflict(k, p)


def test_conflict_when_only_one_side_names_the_concept():
    assert question_conflict("Trump pardons Ghislaine Maxwell",
                             "Ghislaine Maxwell released from prison")


# --- divergence gating ------------------------------------------------------

def test_conflicting_pair_yields_no_divergence():
    k = _k("Who will run for the Democratic nomination Newsom", 0.82)
    p = _p("Will Gavin Newsom win the 2028 Democratic nomination?", 0.18)
    assert find_divergences([k], [p], min_similarity=0.6, min_gap=0.05) == []


def test_genuine_gap_is_reported():
    k = _k("Will the Fed cut rates in September", 0.62)
    p = _p("Will the Fed cut rates in September?", 0.45)
    d = find_divergences([k], [p], min_similarity=0.6, min_gap=0.08)
    assert len(d) == 1
    assert d[0].gap == 0.17 and d[0].buy_yes and d[0].entry_price == 0.45


def test_gap_the_other_way_buys_no():
    k = _k("Will the Fed cut rates in September", 0.30)
    p = _p("Will the Fed cut rates in September?", 0.55)
    d = find_divergences([k], [p], min_similarity=0.6, min_gap=0.08)
    assert not d[0].buy_yes
    assert abs(d[0].entry_price - 0.45) < 1e-9     # 1 - 0.55


def test_small_gap_is_ignored():
    k = _k("Will the Fed cut rates in September", 0.50)
    p = _p("Will the Fed cut rates in September?", 0.46)
    assert find_divergences([k], [p], min_similarity=0.6, min_gap=0.08) == []


def test_different_deadlines_are_different_questions():
    k = _k("Will the Fed cut rates", 0.62, end="2026-12-31T00:00:00Z")
    p = _p("Will the Fed cut rates?", 0.40, end="2027-06-30T00:00:00Z")
    assert find_divergences([k], [p], min_similarity=0.6, min_gap=0.08,
                            max_end_hours=72) == []


def test_extreme_prices_are_skipped():
    k = _k("Will the Fed cut rates in September", 0.99)
    p = _p("Will the Fed cut rates in September?", 0.02)
    assert find_divergences([k], [p], min_similarity=0.6, min_gap=0.08) == []


# --- fetch_kalshi field handling --------------------------------------------
# Kalshi is mid-migration between integer-cents and dollar-string price fields,
# and `expiration_time` is a padded settlement deadline that can lag the real
# event by a week. Both traps are documented in kalshi.py; these tests pin that
# this fetcher actually uses those lessons.

def _kalshi_events(market):
    def fake_fetch(url, params):
        return {"events": [{"event_ticker": "KXFED-26DEC",
                            "title": "Fed decision",
                            "markets": [market]}]}
    return fake_fetch


def test_cents_only_payload_still_quotes():
    # No *_dollars fields at all: bare fields are integer cents.
    m = {"ticker": "KXFED-26DEC-T3", "yes_bid": 40, "yes_ask": 44,
         "close_time": "2026-12-16T19:00:00Z", "volume": 1234}
    (vm,) = fetch_kalshi(fetch=_kalshi_events(m))
    assert vm.yes_price == 0.42
    assert vm.volume == 1234.0


def test_dollar_string_payload_still_quotes():
    m = {"ticker": "KXFED-26DEC-T3", "yes_bid_dollars": "0.40",
         "yes_ask_dollars": "0.44", "close_time": "2026-12-16T19:00:00Z"}
    (vm,) = fetch_kalshi(fetch=_kalshi_events(m))
    assert vm.yes_price == 0.42


def test_end_iso_prefers_event_time_over_settlement_deadline():
    m = {"ticker": "KXFED-26DEC-T3", "yes_bid": 40, "yes_ask": 44,
         "expected_expiration_time": "2026-12-16T19:00:00Z",
         "close_time": "2026-12-16T18:00:00Z",
         "expiration_time": "2026-12-23T19:00:00Z"}   # a week late - the trap
    (vm,) = fetch_kalshi(fetch=_kalshi_events(m))
    assert vm.end_iso == "2026-12-16T19:00:00Z"


def test_unquoted_market_is_dropped():
    m = {"ticker": "KXFED-26DEC-T3", "close_time": "2026-12-16T19:00:00Z"}
    assert fetch_kalshi(fetch=_kalshi_events(m)) == []
