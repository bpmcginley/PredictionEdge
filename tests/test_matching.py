from datetime import datetime, timezone

from predictionedge.kalshi import KalshiMarket
from predictionedge.matching import (
    KalshiSportMarket,
    build_links,
    match_market,
    parse_sports_market,
    parse_sports_title,
)
from predictionedge.normalize import team_score, tokens
from predictionedge.odds import MockOddsProvider, SportEvent

EVENTS = MockOddsProvider().events()  # includes evt-lakers-celtics (Lakers, Celtics)


def test_team_score_subset_and_alias():
    assert team_score("Los Angeles Lakers", "Lakers") == 1.0
    assert team_score("LAL", "Lakers") == 1.0
    assert team_score("Yankees", "Mets") == 0.0


def test_match_basic_orientation():
    k = KalshiSportMarket("KXNBA-LALBOS", "Lakers", "Celtics", yes_team="Lakers")
    link = match_market(k, EVENTS)
    assert link is not None
    assert link.event_id == "evt-lakers-celtics"
    assert link.yes_is_outcome0 is True


def test_match_orientation_flips_with_yes_team():
    k = KalshiSportMarket("KX", "Lakers", "Celtics", yes_team="Celtics")
    link = match_market(k, EVENTS)
    assert link is not None
    assert link.yes_is_outcome0 is False


def test_no_match_for_unknown_teams():
    k = KalshiSportMarket("KX", "Heat", "Knicks", yes_team="Heat")
    assert match_market(k, EVENTS) is None


def test_time_window_blocks_far_apart():
    e = SportEvent("evt-x", "Lakers vs Celtics", ("Lakers", "Celtics"),
                   EVENTS[0].books, start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    k = KalshiSportMarket("KX", "Lakers", "Celtics", yes_team="Lakers",
                          start=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert match_market(k, [e], window_hours=18) is None


def test_parse_title_vs():
    m = parse_sports_title("KX", "Lakers vs Celtics")
    assert m is not None
    assert m.team_a == "Lakers" and m.team_b == "Celtics" and m.yes_team == "Lakers"


def test_parse_title_will_beat():
    m = parse_sports_title("KX", "Will the Lakers beat the Celtics?")
    assert m is not None
    assert tokens(m.team_a) == tokens("Lakers")
    assert tokens(m.team_b) == tokens("Celtics")


def test_build_links_batch():
    ks = [KalshiSportMarket("KXNBA-LALBOS", "Lakers", "Celtics", "Lakers")]
    links = build_links(ks, EVENTS)
    assert len(links) == 1
    assert links[0].event_id == "evt-lakers-celtics"


def test_parse_sports_market_title_matchup_subtitle_orientation():
    # One market per team: the SF market names SF in both sub-titles, so the matchup
    # comes from the title and yes_sub_title gives the YES orientation.
    m = KalshiMarket("KXMLBGAME-26JUN262215ATLSF-SF", "Atlanta vs San Francisco Winner?",
                     0.5, 0.52, 0.48, 0.5, yes_sub_title="San Francisco", no_sub_title="San Francisco")
    pm = parse_sports_market(m)
    assert pm is not None
    assert {pm.team_a, pm.team_b} == {"Atlanta", "San Francisco"}
    assert pm.yes_team == "San Francisco"


def test_parse_sports_market_game_prefix_and_at_separator():
    m = KalshiMarket("KXNBAGAME-26JUN13NYKSAS-NYK", "Game 5: New York at San Antonio Winner?",
                     0.5, 0.52, 0.48, 0.5, yes_sub_title="New York", no_sub_title="New York")
    pm = parse_sports_market(m)
    assert pm is not None
    assert {pm.team_a, pm.team_b} == {"New York", "San Antonio"}
    assert pm.yes_team == "New York"


def test_parse_sports_market_skips_tie():
    m = KalshiMarket("KXEPLGAME-26MAY24LFCBRE-TIE", "Liverpool vs Brentford Winner?",
                     0.3, 0.32, 0.68, 0.7, yes_sub_title="Tie", no_sub_title="")
    assert parse_sports_market(m) is None
