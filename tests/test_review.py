import json
import logging
import os
import tempfile
from dataclasses import replace

from predictionedge.config import Config
from predictionedge.executor import DryRunExecutor
from predictionedge.fees import trade_fee
from predictionedge.kalshi import MockKalshiClient
from predictionedge.ledger import PaperLedger
from predictionedge.odds import MockOddsProvider
from predictionedge.review import review_open_orders
from predictionedge.risk import RiskManager
from predictionedge.state import StateStore
from predictionedge.whales import NeutralWhaleProvider

LOG = logging.getLogger("pe-test")
LOG.addHandler(logging.NullHandler())

# MockOddsProvider consensus for the Lakers event is ~0.537 P(YES).


def _ctx(**over):
    d = tempfile.mkdtemp()
    cfg = replace(Config(), state_db_path=os.path.join(d, "s.db"),
                  paper_ledger_path=os.path.join(d, "l.jsonl"),
                  killswitch_path=os.path.join(d, "STOP"), **over)
    state = StateStore(cfg.state_db_path)
    ledger = PaperLedger(cfg.paper_ledger_path)
    risk = RiskManager(cfg, state)
    ex = DryRunExecutor(cfg, state, ledger, LOG)
    return cfg, state, risk, ex


def _review(cfg, state, risk, ex):
    return review_open_orders(cfg, state, MockKalshiClient(), MockOddsProvider(),
                              NeutralWhaleProvider(), ex, risk, LOG)


def test_review_hedges_on_favourable_move():
    cfg, state, risk, ex = _ctx()
    # Bought YES when P(YES) was 0.40; consensus is now ~0.537 (q ~ 1.34 > 1).
    state.record_order(client_order_id="e1", ticker="KXNBA-LALBOS", side="yes",
                       price=0.40, contracts=25, stake=10.0, entry_event_prob=0.40,
                       status="placed")
    actions = _review(cfg, state, risk, ex)
    assert actions >= 1
    hedges = [r for r in state.active_orders() if r["kind"] == "hedge"]
    assert len(hedges) == 1
    assert hedges[0]["side"] == "no"          # inverse of the YES entry


def test_hedge_is_booked_at_taker_fees():
    """A hedge is priced at the inverse ASK on purpose - it exists to lock the move in
    now - so it is the last order in the system that may be booked at a resting rate.
    This used to guard against `cfg.assume_maker` reaching the fee and reading a quarter
    of the real cost; the flag is gone, and the assertion below is what survives it."""
    cfg, state, risk, ex = _ctx()
    state.record_order(client_order_id="e1", ticker="KXNBA-LALBOS", side="yes",
                       price=0.40, contracts=25, stake=10.0, entry_event_prob=0.40,
                       status="placed")
    _review(cfg, state, risk, ex)
    rows = [json.loads(line) for line
            in open(cfg.paper_ledger_path, encoding="utf-8").read().splitlines() if line]
    hedge = next(r for r in rows if r["note"].startswith("[hedge]"))
    assert hedge["est_fee"] == trade_fee(hedge["price"], hedge["contracts"],
                                         multiplier=cfg.fee_multiplier, maker=False)


def test_review_does_not_double_hedge():
    cfg, state, risk, ex = _ctx()
    state.record_order(client_order_id="e1", ticker="KXNBA-LALBOS", side="yes",
                       price=0.40, contracts=25, stake=10.0, entry_event_prob=0.40,
                       status="placed")
    _review(cfg, state, risk, ex)
    _review(cfg, state, risk, ex)  # second pass must not add another hedge
    hedges = [r for r in state.active_orders() if r["kind"] == "hedge"]
    assert len(hedges) == 1


def test_review_cancels_stale_order():
    cfg, state, risk, ex = _ctx()
    # Bought YES when P(YES) was 0.70; consensus is now ~0.537 -> moved against us.
    state.record_order(client_order_id="e1", ticker="KXNBA-LALBOS", side="yes",
                       price=0.70, contracts=10, stake=7.0, entry_event_prob=0.70,
                       status="placed")
    actions = _review(cfg, state, risk, ex)
    assert actions == 1
    row = state.db.execute(
        "SELECT status FROM orders WHERE client_order_id='e1'"
    ).fetchone()
    assert row["status"] == "cancelled"
