import os
import tempfile
from dataclasses import replace

from predictionedge.config import Config
from predictionedge.edge import Quote, find_edge
from predictionedge.risk import RiskManager
from predictionedge.state import StateStore


def _ctx(**over):
    d = tempfile.mkdtemp()
    cfg = replace(Config(), state_db_path=os.path.join(d, "s.db"),
                  killswitch_path=os.path.join(d, "STOP"), **over)
    state = StateStore(cfg.state_db_path)
    return cfg, state, RiskManager(cfg, state)


def test_preflight_ok_when_clean():
    _, _, risk = _ctx()
    assert risk.preflight().allowed


def test_killswitch_blocks():
    cfg, _, risk = _ctx()
    open(cfg.killswitch_path, "w").close()
    dec = risk.preflight()
    assert not dec.allowed
    assert "killswitch" in dec.reason


def test_daily_loss_blocks():
    cfg, state, risk = _ctx(max_daily_loss=10.0)
    state.add_realized(-10.0)
    assert not risk.preflight().allowed


def test_vet_allows_clean_opportunity():
    cfg, _, risk = _ctx()
    opp = find_edge("T", 0.62, Quote(yes_ask=0.50, no_ask=0.50), cfg)
    assert opp is not None
    assert risk.vet(opp).allowed


def test_vet_blocks_too_good_edge():
    cfg, _, risk = _ctx()
    opp = find_edge("T", 0.95, Quote(yes_ask=0.30, no_ask=0.70), cfg)
    assert opp is not None
    dec = risk.vet(opp)
    assert not dec.allowed
    assert "sanity" in dec.reason


def test_vet_blocks_duplicate_market():
    cfg, state, risk = _ctx()
    opp = find_edge("T", 0.62, Quote(yes_ask=0.50, no_ask=0.50), cfg)
    assert opp is not None
    state.record_order(client_order_id="x", ticker=opp.ticker, side="yes", price=0.5,
                       contracts=1, stake=0.5, entry_event_prob=0.6, status="placed")
    assert not risk.vet(opp).allowed


def test_vet_blocks_exposure_cap():
    cfg, state, risk = _ctx(max_total_exposure=10.0)
    state.record_order(client_order_id="x", ticker="OTHER", side="yes", price=0.5,
                       contracts=18, stake=9.5, entry_event_prob=0.6, status="placed")
    opp = find_edge("T", 0.62, Quote(yes_ask=0.50, no_ask=0.50), cfg)
    assert opp is not None
    assert not risk.vet(opp).allowed  # 9.5 + stake would breach 10.0
