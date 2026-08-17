import dataclasses
import inspect
import json
import logging
import os
import tempfile
from dataclasses import replace

from predictionedge.config import Config
from predictionedge.fees import trade_fee
from predictionedge.kalshi import KalshiMarket
from predictionedge.ledger import PaperLedger
from predictionedge.settle import rested_and_filled, settle_positions
from predictionedge.state import StateStore

LOG = logging.getLogger("pe-test-settle")
LOG.addHandler(logging.NullHandler())


class FakeKalshi:
    def __init__(self, market):
        self._m = market

    def market(self, ticker):
        return self._m if self._m and self._m.ticker == ticker else None


def _ctx(**over):
    d = tempfile.mkdtemp()
    cfg = replace(Config(), state_db_path=os.path.join(d, "s.db"),
                  paper_ledger_path=os.path.join(d, "l.jsonl"),
                  settled_path=os.path.join(d, "bets.jsonl"), **over)
    return cfg, StateStore(cfg.state_db_path), PaperLedger(cfg.paper_ledger_path)


def _open_order(state):
    state.record_order(client_order_id="o1", ticker="T", side="yes", price=0.5,
                       contracts=10, stake=5.0, entry_event_prob=0.6, status="placed")


def test_settle_win_books_profit_and_labels():
    cfg, state, ledger = _ctx()
    _open_order(state)
    # "finalized" ON PURPOSE. These tests used to mock status="settled", a value Kalshi
    # does not return, so they passed for seven weeks against a module that matched
    # nothing in production and never wrote a single labelled bet. A fixture that only
    # asserts your own assumption back at you is worse than no test.
    market = KalshiMarket("T", "t", 0.5, 0.5, 0.5, 0.5, status="finalized", result="yes")
    res = settle_positions(cfg, state, FakeKalshi(market), ledger, LOG)
    assert res["settled"] == 1
    assert state.today_realized_pnl() > 0
    assert not state.has_active_market("T")
    lines = open(cfg.settled_path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 1 and '"won": true' in lines[0]


def test_settle_loss_books_negative():
    cfg, state, ledger = _ctx()
    _open_order(state)
    market = KalshiMarket("T", "t", 0.5, 0.5, 0.5, 0.5, status="finalized", result="no")
    res = settle_positions(cfg, state, FakeKalshi(market), ledger, LOG)
    assert res["settled"] == 1
    assert state.today_realized_pnl() < 0


def test_settle_skips_unsettled():
    cfg, state, ledger = _ctx()
    _open_order(state)
    market = KalshiMarket("T", "t", 0.5, 0.5, 0.5, 0.5, status="open", result="")
    res = settle_positions(cfg, state, FakeKalshi(market), ledger, LOG)
    assert res["settled"] == 0
    assert state.has_active_market("T")


def test_every_terminal_status_settles():
    """Kalshi's vocabulary is the venue's to change; each accepted word must work."""
    for status in ("finalized", "settled", "determined"):
        cfg, state, ledger = _ctx()
        _open_order(state)
        market = KalshiMarket("T", "t", 0.5, 0.5, 0.5, 0.5, status=status, result="yes")
        assert settle_positions(cfg, state, FakeKalshi(market), ledger, LOG)["settled"] == 1


def _settled_row(cfg):
    line = open(cfg.settled_path, encoding="utf-8").read().strip()
    return json.loads(line)


def test_settled_position_is_charged_the_taker_fee():
    """The settle fee is not just a label: it lands in `realized`, which feeds the
    daily-loss breaker and the labelled dataset the backtest scores."""
    cfg, state, ledger = _ctx()
    _open_order(state)                      # 10 contracts @ 0.50
    market = KalshiMarket("T", "t", 0.5, 0.5, 0.5, 0.5, status="finalized", result="yes")
    settle_positions(cfg, state, FakeKalshi(market), ledger, LOG)
    row = _settled_row(cfg)
    taker = trade_fee(0.5, 10, multiplier=cfg.fee_multiplier, maker=False)
    assert row["fee"] == taker
    assert row["fee"] > trade_fee(0.5, 10, multiplier=cfg.fee_multiplier, maker=True)
    assert row["realized"] == round(10 * 0.5 - taker, 4)


def test_the_assume_maker_switch_stays_deleted():
    """`Config.assume_maker` defaulted to True and charged 25% of the taker fee on every
    position - 287 of the 376 rows in the trial's first headline, all in the flattering
    direction. It was deleted outright on 2026-08-17 rather than pinned to False, so this
    is the tripwire that replaces the two-run comparison that used to live here: a dead
    flag whose default is the wrong answer is exactly the shape of thing someone re-wires.

    Both halves matter. Re-adding the field is caught by the first assertion; re-adding
    only the env plumbing - which would resurrect the behaviour without the field being
    obvious in the dataclass - is caught by the second."""
    assert "assume_maker" not in {f.name for f in dataclasses.fields(Config)}
    assert "PE_ASSUME_MAKER" not in inspect.getsource(Config.from_env)
    # The discount has to be earned instead, and nothing in the settle path can grant it.
    assert not rested_and_filled({})


def test_maker_discount_needs_evidence_of_a_rest():
    """The discount is earned per fill or not at all - never assumed for a whole book."""
    assert rested_and_filled({"maker": True})
    assert not rested_and_filled({"maker": False})
    assert not rested_and_filled({})
    # And the shape state.py actually stores: an orders row carries no fill detail,
    # so nothing that exists today can claim the discount.
    cfg, state, _ = _ctx()
    _open_order(state)
    assert not any(rested_and_filled(r) for r in state.active_orders())


def test_decided_but_unknown_status_is_skipped_loudly(caplog):
    """The failure mode that cost seven weeks was silence, so the warning is the test."""
    cfg, state, ledger = _ctx()
    _open_order(state)
    market = KalshiMarket("T", "t", 0.5, 0.5, 0.5, 0.5, status="closed_out", result="yes")
    with caplog.at_level(logging.WARNING, logger=LOG.name):
        res = settle_positions(cfg, state, FakeKalshi(market), ledger, LOG)
    assert res["settled"] == 0                  # do not book cash we are unsure of
    assert state.has_active_market("T")         # and do not lose the position either
    assert any("closed_out" in r.getMessage() for r in caplog.records)
