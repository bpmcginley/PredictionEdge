from predictionedge.flow import classify, find_spikes
from predictionedge.polymarket import MockPolymarketDataClient, Trade


def _t(title, size, *, side="BUY", ts=1000, slug="", event_slug="", cid="0x", price=0.3):
    """`size` is CONTRACTS, as it is on the real feed; the bet cost size*price."""
    return Trade(wallet="0xabc123def456", name="whale", side=side, size=size, price=price,
                 ts=ts, title=title, outcome="Yes", condition_id=cid,
                 event_slug=event_slug, slug=slug)


def test_classify_scalp_sports_event():
    assert classify(_t("Bitcoin Up or Down - 10:15AM", 50000, slug="btc-updown-15m")) == "scalp"
    assert classify(_t("Lakers vs Celtics", 50000, slug="nba-lal-bos")) == "sports"
    assert classify(_t("Will the US invade Venezuela before 2027?", 50000)) == "event"
    # A political head-to-head must NOT be misread as sports.
    assert classify(_t("Trump vs Newsom 2028", 50000, slug="potus-2028")) == "event"


def test_find_spikes_drops_scalp_and_sports_ranks_by_dollars():
    trades = [
        _t("BTC up or down 15m", 100000, slug="btc-updown-15m"),          # scalp
        _t("Lakers vs Celtics", 80000, slug="nba-lal-bos"),              # sports
        _t("Will the US invade Venezuela before 2027?", 60000, cid="0xV"),
        _t("Fed cuts in July?", 40000, cid="0xF"),
    ]
    spikes = find_spikes(MockPolymarketDataClient(trades=trades), min_usd=5000, now_ts=2000)
    assert [s.trade.condition_id for s in spikes] == ["0xV", "0xF"]
    assert all(s.category == "event" for s in spikes)


def test_min_usd_threshold():
    """The bar is CASH, size*price, the way the live feed applies filterType=CASH.

    Both fixtures are priced at 30c, so 0xB's 10,000 contracts are $3,000 and must not
    clear a $5,000 bar - it would have, back when the bar was compared to the raw
    contract count.
    """
    trades = [_t("Will X happen?", 30000, cid="0xA"), _t("Will Y happen?", 10000, cid="0xB")]
    spikes = find_spikes(MockPolymarketDataClient(trades=trades), min_usd=5000)
    assert [s.trade.condition_id for s in spikes] == ["0xA"]


def test_include_sports_flag():
    trades = [_t("Lakers vs Celtics", 80000, slug="nba-lal-bos", cid="0xS"),
              _t("Will X happen?", 60000, cid="0xE")]
    spikes = find_spikes(MockPolymarketDataClient(trades=trades), min_usd=5000,
                         include_sports=True)
    assert {s.trade.condition_id for s in spikes} == {"0xS", "0xE"}


def test_minutes_ago_computed():
    trades = [_t("Will X happen?", 50000, ts=1000)]
    spikes = find_spikes(MockPolymarketDataClient(trades=trades), min_usd=5000, now_ts=1600)
    assert spikes[0].minutes_ago == 10.0


# --- units: "biggest" means dollars ------------------------------------------
# The feed reports CONTRACTS. Ranking on that count put cheap longshots at the top of
# a list the dashboard truncates, so the order decided which bets got published at all.

def test_biggest_means_dollars_not_contracts():
    """Count-order and cash-order are deliberately opposite here.

    By contracts: CHEAP (300,000) beats DEAR (20,000) by 15x. By money: CHEAP is
    300,000 at 3c = $9,000 and DEAR is 20,000 at 80c = $16,000, so DEAR is the bigger
    bet and must come first. If this ever flips, the sort key went back to a count.
    """
    trades = [_t("Will a longshot happen?", 300_000, price=0.03, cid="0xCHEAP"),
              _t("Will X happen?", 20_000, price=0.80, cid="0xDEAR")]
    spikes = find_spikes(MockPolymarketDataClient(trades=trades), min_usd=5000)
    assert [s.trade.condition_id for s in spikes] == ["0xDEAR", "0xCHEAP"]
    assert [round(s.usd) for s in spikes] == [16_000, 9_000]


def test_a_truncated_list_keeps_the_biggest_bets_not_the_biggest_counts():
    """The dashboard publishes `find_spikes(...)[:18]`, so order decides who is DROPPED.

    Three eligible bets, two slots - the mirror of that slice. Ranked by contract count
    the $9k longshot would take a slot and the $16k bet would fall off the end, which is
    a lost signal rather than a shuffled one.
    """
    trades = [_t("Longshot A", 300_000, price=0.03, cid="0xCHEAP"),     # $9,000
              _t("Will X happen?", 20_000, price=0.80, cid="0xDEAR"),   # $16,000
              _t("Will Y happen?", 40_000, price=0.55, cid="0xMID")]    # $22,000
    top2 = find_spikes(MockPolymarketDataClient(trades=trades), min_usd=5000)[:2]
    assert [s.trade.condition_id for s in top2] == ["0xMID", "0xDEAR"]
    assert "0xCHEAP" not in [s.trade.condition_id for s in top2]


# --- esports: the panel honours the cut the project already made ---------------

def test_classify_uses_the_project_wide_esports_test_not_a_local_copy():
    """`_SPORTS` says "league of legends"; Polymarket publishes "LoL:".

    That gap is why this module must not carry its own pattern - the titles that reach
    the feed are the abbreviated ones, and a private copy of the rule silently disagreed
    with `copytrade.is_esports`, which is what the rest of the project rejects on.
    """
    assert classify(_t("LoL: SK Gaming vs Fnatic", 50000, event_slug="lol-skg-fnc-2026-08-16")) \
        == "esports"
    # Derivative markets on an esports fixture name no game at all - only the slug does.
    assert classify(_t("Game Handicap: NS (-1.5) vs DN SOOPers (+1.5)", 50000,
                       event_slug="lol-dnf-ns-2026-08-12")) == "esports"


def test_esports_spikes_are_dropped_unless_the_flag_says_otherwise():
    """One switch, `Config.esports_enabled`, decides this - passed in, never read here.

    Off (the project's setting since esports was retired) the LoL fixture must not reach
    a world-events panel at all; on, it comes back. Nothing else about the spike changes,
    so a failure here means the flag stopped being the thing that decides.
    """
    trades = [_t("LoL: SK Gaming vs Fnatic", 50_000, event_slug="lol-skg-fnc", cid="0xLOL"),
              _t("Will the US invade Venezuela before 2027?", 40_000, cid="0xV")]
    client = MockPolymarketDataClient(trades=trades)

    off = find_spikes(client, min_usd=5000)
    assert [s.trade.condition_id for s in off] == ["0xV"]

    on = find_spikes(client, min_usd=5000, esports_enabled=True)
    assert [s.trade.condition_id for s in on] == ["0xLOL", "0xV"]
    assert on[0].category == "esports"


def test_showing_sports_does_not_smuggle_esports_back_in():
    """Two independent switches: ball games are noise, esports are a retired market."""
    trades = [_t("LoL: SK Gaming vs Fnatic", 50_000, event_slug="lol-skg-fnc", cid="0xLOL"),
              _t("Lakers vs Celtics", 50_000, slug="nba-lal-bos", cid="0xNBA")]
    spikes = find_spikes(MockPolymarketDataClient(trades=trades), min_usd=5000,
                         include_sports=True)
    assert [s.trade.condition_id for s in spikes] == ["0xNBA"]


def test_spike_carries_the_cash_the_bet_cost():
    """Callers get the dollars as a named field, so nobody has to re-derive them."""
    trades = [_t("Will X happen?", 20_000, price=0.45, cid="0xA")]
    s = find_spikes(MockPolymarketDataClient(trades=trades), min_usd=5000)[0]
    assert s.usd == 9_000
    assert s.usd != s.trade.size          # the two are different numbers, on purpose
    assert s.usd == s.trade.size * s.trade.price
