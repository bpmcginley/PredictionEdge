from dataclasses import replace

from predictionedge.config import Config
from predictionedge.edge import Quote, find_edge
from predictionedge.fees import trade_fee

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


# --- the bar is charged at TAKER rates ---------------------------------------
# `_evaluate_side`'s fee is a THRESHOLD, not bookkeeping: it decides which tickets
# get published. Charged at 25% of taker it sat up to 1.31c/ct below the truth -
# 44% of the 3c `min_edge` - and the error only ever admitted bets. These pin the
# corrected bar, and the first one fails the moment the discount comes back.

_MID = Quote(yes_ask=0.50, no_ask=0.50)   # 50c: where the taker fee peaks, 1.75c/ct


def test_bar_refuses_an_edge_that_only_a_maker_discount_would_pass():
    """A 4c gap at 50c nets 3.56c against a maker fee and 2.25c against the real one."""
    assert find_edge("T", fair_prob=0.54, quote=_MID, cfg=CFG) is None


def test_bar_passes_an_edge_that_covers_the_real_fee():
    """The same test from the other side, so the case above is not passing on a typo:
    6c gross clears 3c net after the full 1.75c taker fee."""
    opp = find_edge("T", fair_prob=0.56, quote=_MID, cfg=CFG)
    assert opp is not None
    assert opp.edge_per_contract >= CFG.min_edge


def test_reported_fee_is_the_fee_the_edge_was_tested_against():
    """One number behind the decision and a different one on the ticket would make
    the ledger and `describe()` describe a gate that never ran.

    With `Config.assume_maker` deleted there is no switch left to flip, so the pair of
    tests above - 4c refused, 6c admitted - is now the whole tripwire on the bar's
    height. `tests/test_settle.py` holds the one that stops the flag coming back."""
    opp = find_edge("T", fair_prob=0.70, quote=Quote(yes_ask=0.60, no_ask=0.42), cfg=CFG)
    assert opp is not None
    assert opp.est_fee == trade_fee(opp.price, opp.contracts,
                                    multiplier=CFG.fee_multiplier, maker=False)
    assert opp.est_fee > trade_fee(opp.price, opp.contracts,
                                   multiplier=CFG.fee_multiplier, maker=True)
