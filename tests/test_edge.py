from dataclasses import replace

from predictionedge.config import Config
from predictionedge.edge import Quote, find_edge

CFG = Config()  # defaults: min_edge=0.03, kelly=0.25, per_market_cap=0.05, bankroll=500


def test_buy_yes_when_fair_far_above_ask():
    opp = find_edge("T", fair_prob=0.70, quote=Quote(yes_ask=0.60, no_ask=0.42), cfg=CFG)
    assert opp is not None
    assert opp.side == "yes"
    assert opp.contracts > 0
    assert opp.edge_per_contract > CFG.min_edge
    assert opp.expected_value > 0


def test_buy_no_when_yes_overpriced():
    # Fair P(YES)=0.20, so NO is cheap at 0.60 and YES expensive at 0.85.
    opp = find_edge("T", fair_prob=0.20, quote=Quote(yes_ask=0.85, no_ask=0.60), cfg=CFG)
    assert opp is not None
    assert opp.side == "no"


def test_no_edge_returns_none():
    opp = find_edge("T", fair_prob=0.50, quote=Quote(yes_ask=0.52, no_ask=0.50), cfg=CFG)
    assert opp is None


def test_per_market_cap_limits_size():
    big = replace(CFG, bankroll=100_000)
    opp = find_edge("T", fair_prob=0.70, quote=Quote(yes_ask=0.60, no_ask=0.42), cfg=big)
    assert opp is not None
    # stake must respect the 5% per-market cap (plus rounding down to whole contracts)
    assert opp.stake <= big.bankroll * big.per_market_max_fraction + 0.60


def test_threshold_blocks_thin_edge():
    strict = replace(CFG, min_edge=0.20)
    opp = find_edge("T", fair_prob=0.70, quote=Quote(yes_ask=0.60, no_ask=0.42), cfg=strict)
    assert opp is None  # 9.6c edge no longer clears a 20c bar
