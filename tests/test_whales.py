from predictionedge.fairvalue import blended_fair_prob
from predictionedge.polymarket import MockPolymarketDataClient, WalletStat
from predictionedge.whales import (
    PolymarketWhaleProvider,
    SmartMoneyConfig,
    SmartWalletScorer,
)


def test_scorer_excludes_small_sample_and_churn():
    smart = SmartWalletScorer().smart_set(MockPolymarketDataClient().leaderboard())
    assert "0xSHARP1" in smart and "0xSHARP2" in smart
    assert "0xLUCKY" not in smart   # only 4 resolved markets -> not enough sample
    assert "0xMM" not in smart      # huge volume, tiny ROI -> market-maker, excluded


def test_provider_signal_prob_is_polymarket_price():
    prices = {"PM1": (0.62, 5_000_000, 200_000)}   # (yes_price, volume, liquidity)
    prov = PolymarketWhaleProvider(MockPolymarketDataClient(prices=prices),
                                   SmartWalletScorer(), {"KXT": "PM1"})
    sig = prov.signal_for("KXT")
    assert sig is not None
    assert sig.prob == 0.62                  # the informed market's probability
    assert 0.0 < sig.confidence <= 1.0


def test_provider_none_when_unmapped():
    prov = PolymarketWhaleProvider(MockPolymarketDataClient(), SmartWalletScorer(), {})
    assert prov.signal_for("KXT") is None


def test_provider_none_without_price():
    prov = PolymarketWhaleProvider(MockPolymarketDataClient(), SmartWalletScorer(),
                                   {"KXT": "PM1"})
    assert prov.signal_for("KXT") is None    # mapped but no price data


def test_signal_pulls_fair_value():
    prices = {"PM1": (0.10, 5_000_000, 200_000)}   # informed market says 0.10
    prov = PolymarketWhaleProvider(MockPolymarketDataClient(prices=prices),
                                   SmartWalletScorer(), {"KXT": "PM1"})
    sig = prov.signal_for("KXT")
    assert sig is not None and sig.prob == 0.10
    assert blended_fair_prob(0.6, sig, whale_weight=0.5) < 0.6   # pulls fair down


def test_is_smart_requires_min_pnl():
    scorer = SmartWalletScorer(SmartMoneyConfig(min_realized_pnl=1_000_000))
    assert scorer.smart_set(MockPolymarketDataClient().leaderboard()) == set()


def test_enrichment_fills_sample_and_qualifies_wallet():
    # Leaderboard entry has unknown sample (resolved=0); enrichment fills it from
    # 40 closed positions (26 wins -> 65% win rate, clears the 30-market floor).
    lb = [WalletStat("0xW", realized_pnl=50_000, resolved_markets=0,
                     volume=500_000, win_rate=0.0)]
    closed = {"0xW": [100.0] * 26 + [-50.0] * 14}
    client = MockPolymarketDataClient(leaderboard=lb, closed=closed)
    prov = PolymarketWhaleProvider(client, SmartWalletScorer(), {}, enrich=True)
    assert "0xW" in prov._smart_addresses()


def test_enrichment_excludes_small_sample():
    lb = [WalletStat("0xW", realized_pnl=50_000, resolved_markets=0,
                     volume=500_000, win_rate=0.0)]
    closed = {"0xW": [100.0] * 5}  # only 5 resolved -> below the 30 floor
    client = MockPolymarketDataClient(leaderboard=lb, closed=closed)
    prov = PolymarketWhaleProvider(client, SmartWalletScorer(), {}, enrich=True)
    assert "0xW" not in prov._smart_addresses()
