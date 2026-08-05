from predictionedge.copytrade import (
    CopySignal,
    copy_order_params,
    find_copy_signals,
    smart_universe,
)
from predictionedge.polymarket import MockPolymarketDataClient, Trade
from predictionedge.polymarket_us import BUY_NO, BUY_YES, PMUSMarket
from predictionedge.whales import SmartWalletScorer


def _t(wallet, market, outcome, size, *, side="BUY", price=0.4, ts=1000, title="Mkt", slug=""):
    return Trade(wallet=wallet, name="", side=side, size=size, price=price, ts=ts,
                 title=title, outcome=outcome, condition_id=market, event_slug=slug, slug="")


# The mock leaderboard's smart wallets are 0xSHARP1 and 0xSHARP2.
def _scorer():
    return SmartWalletScorer()


def test_smart_universe_from_leaderboard():
    smart = smart_universe(MockPolymarketDataClient(), _scorer(), ("OVERALL", "SPORTS"))
    assert "0xSHARP1" in smart and "0xSHARP2" in smart
    assert "0xLUCKY" not in smart   # tiny sample excluded


def test_copy_signal_groups_smart_buys():
    trades = [
        _t("0xSHARP1", "PMA", "Yes", 20000, price=0.40),
        _t("0xSHARP2", "PMA", "Yes", 15000, price=0.45),   # same market+outcome -> group
        _t("0xRANDO", "PMB", "No", 50000),                 # not a smart wallet -> ignored
        _t("0xSHARP1", "PMC", "Yes", 30000, side="SELL"),  # sell -> ignored
        _t("0xSHARP1", "PMD", "Yes", 12000, price=0.97),   # near-resolved -> filtered
    ]
    sigs = find_copy_signals(MockPolymarketDataClient(trades=trades), _scorer(),
                             min_usd=10000, max_price=0.90, now_ts=2000)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.market_id == "PMA" and s.outcome == "Yes"
    assert s.n_wallets == 2 and abs(s.total_usd - 35000) < 1e-6
    assert 0.41 < s.avg_price < 0.43          # volume-weighted ~0.421


def test_min_wallets_gate():
    trades = [_t("0xSHARP1", "PMA", "Yes", 20000)]
    sigs = find_copy_signals(MockPolymarketDataClient(trades=trades), _scorer(),
                             min_usd=10000, min_wallets=2, now_ts=2000)
    assert sigs == []


def test_no_signals_when_no_smart_buys():
    trades = [_t("0xRANDO", "PMB", "No", 50000)]
    assert find_copy_signals(MockPolymarketDataClient(trades=trades), _scorer(),
                             min_usd=10000, now_ts=2000) == []


_SIG_YES = CopySignal("cid", "T", "Yes", 2, 30000, 0.40, 5.0, slug="mkt")
_SIG_NO = CopySignal("cid", "T", "No", 2, 30000, 0.40, 5.0, slug="mkt")
_MKT = PMUSMarket("mkt", yes_bid=0.50, yes_ask=0.52, last_px=0.51)


def test_copy_order_params_buy_yes():
    intent, price, qty = copy_order_params(_SIG_YES, _MKT, size_usd=10)
    assert intent == BUY_YES and price == 0.52 and qty == int(10 / 0.52)


def test_copy_order_params_buy_no_uses_no_price():
    intent, price, qty = copy_order_params(_SIG_NO, _MKT, size_usd=10)
    assert intent == BUY_NO and price == 0.50   # 1 - yes_bid(0.50)


def test_copy_order_params_none_market():
    assert copy_order_params(_SIG_YES, None) is None


def test_copy_order_params_out_of_band():
    deep = PMUSMarket("mkt", yes_bid=0.0, yes_ask=0.99, last_px=0.99)
    assert copy_order_params(_SIG_YES, deep, max_price=0.90) is None
