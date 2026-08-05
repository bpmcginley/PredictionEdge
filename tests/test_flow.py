from predictionedge.flow import classify, find_spikes
from predictionedge.polymarket import MockPolymarketDataClient, Trade


def _t(title, size, *, side="BUY", ts=1000, slug="", event_slug="", cid="0x"):
    return Trade(wallet="0xabc123def456", name="whale", side=side, size=size, price=0.3,
                 ts=ts, title=title, outcome="Yes", condition_id=cid,
                 event_slug=event_slug, slug=slug)


def test_classify_scalp_sports_event():
    assert classify(_t("Bitcoin Up or Down - 10:15AM", 50000, slug="btc-updown-15m")) == "scalp"
    assert classify(_t("Lakers vs Celtics", 50000, slug="nba-lal-bos")) == "sports"
    assert classify(_t("Will the US invade Venezuela before 2027?", 50000)) == "event"
    # A political head-to-head must NOT be misread as sports.
    assert classify(_t("Trump vs Newsom 2028", 50000, slug="potus-2028")) == "event"


def test_find_spikes_drops_scalp_and_sports_ranks_by_size():
    trades = [
        _t("BTC up or down 15m", 100000, slug="btc-updown-15m"),          # scalp
        _t("Lakers vs Celtics", 80000, slug="nba-lal-bos"),              # sports
        _t("Will the US invade Venezuela before 2027?", 60000, cid="0xV"),
        _t("Fed cuts in July?", 40000, cid="0xF"),
    ]
    spikes = find_spikes(MockPolymarketDataClient(trades=trades), min_usd=25000, now_ts=2000)
    assert [s.trade.condition_id for s in spikes] == ["0xV", "0xF"]
    assert all(s.category == "event" for s in spikes)


def test_min_usd_threshold():
    trades = [_t("Will X happen?", 30000, cid="0xA"), _t("Will Y happen?", 10000, cid="0xB")]
    spikes = find_spikes(MockPolymarketDataClient(trades=trades), min_usd=25000)
    assert [s.trade.condition_id for s in spikes] == ["0xA"]


def test_include_sports_flag():
    trades = [_t("Lakers vs Celtics", 80000, slug="nba-lal-bos", cid="0xS"),
              _t("Will X happen?", 60000, cid="0xE")]
    spikes = find_spikes(MockPolymarketDataClient(trades=trades), min_usd=25000,
                         include_sports=True)
    assert {s.trade.condition_id for s in spikes} == {"0xS", "0xE"}


def test_minutes_ago_computed():
    trades = [_t("Will X happen?", 50000, ts=1000)]
    spikes = find_spikes(MockPolymarketDataClient(trades=trades), min_usd=25000, now_ts=1600)
    assert spikes[0].minutes_ago == 10.0
