import os
import tempfile

from predictionedge.state import StateStore


def _store() -> StateStore:
    d = tempfile.mkdtemp()
    return StateStore(os.path.join(d, "s.db"))


def _order(s, coid="a", ticker="T1", status="placed", stake=5.0):
    s.record_order(client_order_id=coid, ticker=ticker, side="yes", price=0.5,
                   contracts=10, stake=stake, entry_event_prob=0.6, status=status)


def test_record_and_exposure():
    s = _store()
    _order(s)
    assert s.open_position_count() == 1
    assert abs(s.open_exposure() - 5.0) < 1e-9
    assert s.has_active_market("T1")
    assert not s.has_active_market("T2")


def test_cancel_frees_exposure():
    s = _store()
    _order(s)
    s.update_order_status("a", "cancelled")
    assert s.open_position_count() == 0
    assert s.open_exposure() == 0.0
    assert not s.has_active_market("T1")


def test_distinct_markets_counted_once():
    s = _store()
    _order(s, coid="a", ticker="T1")
    _order(s, coid="b", ticker="T1")  # same market, second order
    _order(s, coid="c", ticker="T2")
    assert s.open_position_count() == 2
    assert abs(s.open_exposure() - 15.0) < 1e-9


def test_pnl_accumulates():
    s = _store()
    assert s.today_realized_pnl() == 0.0
    s.add_realized(-3.0)
    s.add_realized(-2.0)
    assert abs(s.today_realized_pnl() + 5.0) < 1e-9


def test_recent_orders_newest_first():
    s = _store()
    _order(s, coid="a", ticker="T1")
    _order(s, coid="b", ticker="T2")
    rows = s.recent_orders(limit=10)
    assert len(rows) == 2
    assert {r["client_order_id"] for r in rows} == {"a", "b"}


def test_meta_roundtrip():
    s = _store()
    assert s.get_meta("k", "def") == "def"
    s.set_meta("k", "v")
    assert s.get_meta("k") == "v"
