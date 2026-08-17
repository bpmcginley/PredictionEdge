"""The dashboard's whale panel publishes MONEY under a heading that promises money.

`Trade.size` off the Polymarket feed is a contract count and a contract costs its
price, so the two numbers differ by 1/price - 1.9x at the median 53c fill and 33x on a
3c longshot. The panel rendered the count with a "$" in front of it under a "≥ $25k"
heading, which is the one place on the board where a units slip is not a rounding
detail: it is the published claim about how big the smart money's bet was.
"""

from pathlib import Path

from predictionedge import dashboard
from predictionedge.config import Config
from predictionedge.polymarket import MockPolymarketDataClient, Trade

_TEMPLATE = Path(dashboard.__file__).with_name("dashboard.html")


class _Whales:
    """Just enough of a whale provider for the spike panel: it needs the client only."""

    def __init__(self, client):
        self.client = client
        self.scorer = None


def _trade(size, price, cid="0xA"):
    return Trade(wallet="0xabc123def456", name="whale", side="BUY", size=size,
                 price=price, ts=1000, title="Will the US invade Venezuela before 2027?",
                 outcome="Yes", condition_id=cid, event_slug="venezuela", slug="ven")


def _snapshot(monkeypatch, trades, tmp_path):
    client = MockPolymarketDataClient(trades=trades)
    monkeypatch.setattr(dashboard, "build_whale_provider",
                        lambda cfg, **kw: _Whales(client))
    monkeypatch.setattr(dashboard, "end_dates", lambda cids: {})
    monkeypatch.setattr("predictionedge.whale_edge.find_whale_edges",
                        lambda cfg, kalshi, whales: [])
    cfg = Config(state_db_path=str(tmp_path / "state.db"),
                 paper_ledger_path=str(tmp_path / "ledger.jsonl"),
                 heartbeat_path=str(tmp_path / "heartbeat.json"),
                 copytrade_enabled=False)
    return dashboard.build_snapshot(cfg, force_mock=True)


def test_the_published_size_is_the_cash_the_bet_cost(monkeypatch, tmp_path):
    # 50,000 contracts at 80c is a $40,000 bet. Publishing 50,000 under a dollar sign
    # overstates it by 1/0.8, and the error grows as the price falls.
    snap = _snapshot(monkeypatch, [_trade(50_000, 0.80)], tmp_path)
    spike = snap["spikes"][0]
    assert spike["usd"] == 40_000
    assert spike["contracts"] == 50_000       # kept, but never the number with the $
    assert "size" not in spike                # the ambiguous name is gone for good


def test_a_cheap_longshot_no_longer_looks_like_the_biggest_bet(monkeypatch, tmp_path):
    # The failure at its worst: 1,000,000 contracts at 3c is a $30,000 bet, and it was
    # published as "$1,000,000" - twenty times the contract count of the $40,000 bet
    # beside it, and so the biggest number on a panel about who bet the most money.
    snap = _snapshot(monkeypatch, [_trade(1_000_000, 0.03, cid="0xCHEAP"),
                                   _trade(50_000, 0.80, cid="0xDEAR")], tmp_path)
    assert [s["usd"] for s in snap["spikes"]] == [40_000, 30_000]
    assert [s["contracts"] for s in snap["spikes"]] == [50_000, 1_000_000]


def test_the_panel_renders_the_cash_field_and_not_the_contract_count():
    """The bug lived in the template, so the template is what has to be asserted.

    A page that prints `$${s.size}` is wrong however correct the payload is, and the
    payload no longer carries `size` at all - so this pins the pairing rather than
    trusting that the two halves were changed together.
    """
    html = _TEMPLATE.read_text(encoding="utf-8")
    assert "$${Number(s.usd).toLocaleString()}" in html
    assert "s.size" not in html
    # The heading is a cash bar - `find_spikes(min_usd=25000)` filters on size*price -
    # so it described the panel correctly all along and still does.
    assert "live ≥ $25k" in html
