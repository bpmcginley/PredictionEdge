import logging
import os
import tempfile
from dataclasses import replace

from predictionedge.config import Config
from predictionedge.kalshi import KalshiMarket
from predictionedge.ledger import PaperLedger
from predictionedge.settle import settle_positions
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
    market = KalshiMarket("T", "t", 0.5, 0.5, 0.5, 0.5, status="settled", result="yes")
    res = settle_positions(cfg, state, FakeKalshi(market), ledger, LOG)
    assert res["settled"] == 1
    assert state.today_realized_pnl() > 0
    assert not state.has_active_market("T")
    lines = open(cfg.settled_path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 1 and '"won": true' in lines[0]


def test_settle_loss_books_negative():
    cfg, state, ledger = _ctx()
    _open_order(state)
    market = KalshiMarket("T", "t", 0.5, 0.5, 0.5, 0.5, status="settled", result="no")
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
