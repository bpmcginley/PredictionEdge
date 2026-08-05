from dataclasses import replace

from predictionedge.config import Config
from predictionedge.kalshi import KalshiMarket
from predictionedge.polymarket import MarketHolder, MockPolymarketDataClient, WalletStat
from predictionedge.whale_edge import find_whale_edges
from predictionedge.whales import (
    NeutralWhaleProvider,
    PolymarketWhaleProvider,
    SmartWalletScorer,
)


class FakeKalshi:
    def __init__(self, markets):
        self._m = markets

    def market(self, ticker):
        return self._m.get(ticker)


def _provider(yes_price=0.62):
    lb = [WalletStat("0xS1", 120_000, 180, 2_000_000, 0.61),
          WalletStat("0xS2", 80_000, 120, 1_000_000, 0.60)]
    holders = [MarketHolder("0xS1", yes_size=10000, no_size=0),
               MarketHolder("0xS2", yes_size=10000, no_size=0)]
    client = MockPolymarketDataClient(leaderboard=lb, holders={"PMX": holders},
                                      prices={"PMX": (yes_price, 5_000_000, 200_000)})
    return PolymarketWhaleProvider(client, SmartWalletScorer(), {"KXPOL": "PMX"}, enrich=False)


_MARKET = {"KXPOL": KalshiMarket("KXPOL", "Will X happen?", 0.53, 0.55, 0.45, 0.47)}


def test_whale_edge_found_when_polymarket_diverges():
    # Polymarket prices it 0.62; Kalshi YES is 0.55 -> a YES edge.
    edges = find_whale_edges(Config(), FakeKalshi(_MARKET), _provider(0.62))
    assert len(edges) == 1
    assert edges[0].opp.side == "yes"
    assert edges[0].signal.prob == 0.62
    assert edges[0].signal.confidence >= Config().whale_primary_min_conf


def test_no_edge_when_confidence_below_threshold():
    cfg = replace(Config(), whale_primary_min_conf=0.99)
    assert find_whale_edges(cfg, FakeKalshi(_MARKET), _provider(0.62)) == []


def test_disabled_returns_empty():
    cfg = replace(Config(), whale_primary=False)
    assert find_whale_edges(cfg, FakeKalshi(_MARKET), _provider(0.62)) == []


def test_neutral_provider_has_no_map():
    assert find_whale_edges(Config(), FakeKalshi(_MARKET), NeutralWhaleProvider()) == []
