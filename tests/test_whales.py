from predictionedge.fairvalue import blended_fair_prob
from predictionedge.polymarket import MockPolymarketDataClient, WalletStat
from predictionedge.whales import (
    PolymarketWhaleProvider,
    SmartMoneyConfig,
    SmartWalletScorer,
    WalletProfiler,
)


class _Fetch:
    """Records every call so the tests can prove the cache is actually saving them."""

    def __init__(self, values=None, boom=False):
        self.values = values or {}
        self.boom = boom
        self.calls = []

    def __call__(self, url, params):
        self.calls.append(params.get("user"))
        if self.boom:
            raise RuntimeError("data api down")
        return [{"user": params["user"], "value": self.values[params["user"]]}]


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


# --- E4.1 wallet bankroll lookups --------------------------------------------
# /value is one HTTP round trip per wallet and does not batch (verified 2026-08-06:
# repeated ?user= params return only the last, comma-joined 400s). Across a full feed
# that is the difference between a cron that finishes and one that doesn't.

def _profiler(fetch, **kw):
    return WalletProfiler(fetch=fetch, cache_path=None, **kw)


def test_profiler_reads_wallet_value():
    f = _Fetch({"0xA": 250_000.0})
    assert _profiler(f).bankrolls(["0xA"]) == {"0xA": 250_000.0}


def test_profiler_asks_once_per_wallet_per_run():
    """A per-signal round trip across ~120 signals will not fit a 15-minute cron."""
    f = _Fetch({"0xA": 1.0, "0xB": 2.0})
    p = _profiler(f)
    p.bankrolls(["0xA", "0xB", "0xA"])
    p.bankrolls(["0xA", "0xB"])           # second sweep is served from memory
    assert sorted(f.calls) == ["0xA", "0xB"]


def test_profiler_refetches_after_the_ttl():
    clock = {"t": 1000.0}
    f = _Fetch({"0xA": 1.0})
    p = _profiler(f, ttl_s=100.0, now_fn=lambda: clock["t"])
    p.bankrolls(["0xA"])
    clock["t"] += 101.0
    p.bankrolls(["0xA"])
    assert f.calls == ["0xA", "0xA"]


def test_profiler_returns_unknown_not_zero_on_failure():
    """Fail closed: a dead endpoint must not read as 'this wallet holds nothing'."""
    assert _profiler(_Fetch(boom=True)).bankrolls(["0xA"]) == {}


def test_profiler_treats_an_empty_book_as_unknown():
    """Zero open value is no evidence of wallet size, and it is a division by zero."""
    assert _profiler(_Fetch({"0xA": 0.0})).bankrolls(["0xA"]) == {}


def test_profiler_bounds_its_fan_out():
    f = _Fetch({f"0x{i}": 1.0 for i in range(10)})
    p = _profiler(f, max_wallets=3)
    p.bankrolls([f"0x{i}" for i in range(10)])
    assert len(f.calls) == 3


class _Activity:
    """/activity?sortDirection=ASC - the oldest event, in one call instead of paging."""

    def __init__(self, rows_by_user):
        self.rows = rows_by_user
        self.calls = []

    def __call__(self, url, params):
        self.calls.append(params)
        return self.rows.get(params["user"], [])


def test_first_seen_reads_the_oldest_activity():
    f = _Activity({"0xA": [{"timestamp": 1_700_000_000}]})
    assert WalletProfiler(fetch=f, cache_path=None).first_seen(["0xA"]) == {"0xA": 1_700_000_000}
    assert f.calls[0]["sortDirection"] == "ASC"   # DESC would date the wallet to today


def test_empty_history_is_unknown_not_brand_new():
    """The trap: 'no rows' reading as age zero would make every unreadable wallet a buy."""
    assert WalletProfiler(fetch=_Activity({}), cache_path=None).first_seen(["0xA"]) == {}


def test_first_seen_is_not_refetched_within_the_run():
    f = _Activity({"0xA": [{"timestamp": 1_700_000_000}]})
    p = WalletProfiler(fetch=f, cache_path=None)
    p.first_seen(["0xA"])
    p.first_seen(["0xA"])
    assert len(f.calls) == 1


def test_bankroll_and_age_do_not_share_a_cache_slot():
    """Two different questions about one wallet; answering one is not answering both."""
    seen = []

    def fetch(url, params):
        seen.append(url)
        if url.endswith("/value"):
            return [{"user": params["user"], "value": 500.0}]
        return [{"timestamp": 1_700_000_000}]

    p = WalletProfiler(fetch=fetch, cache_path=None)
    assert p.bankrolls(["0xA"]) == {"0xA": 500.0}
    assert p.first_seen(["0xA"]) == {"0xA": 1_700_000_000}
    assert sum(1 for u in seen if u.endswith("/value")) == 1
    assert sum(1 for u in seen if u.endswith("/activity")) == 1


def test_profiler_cache_survives_a_restart(tmp_path):
    """The smart-wallet set barely moves run to run, so a cold start should be rare."""
    path = str(tmp_path / "profiles.json")
    f1 = _Fetch({"0xA": 500.0})
    WalletProfiler(fetch=f1, cache_path=path).bankrolls(["0xA"])
    f2 = _Fetch({"0xA": 500.0})
    assert WalletProfiler(fetch=f2, cache_path=path).bankrolls(["0xA"]) == {"0xA": 500.0}
    assert f2.calls == []


def test_enrichment_excludes_small_sample():
    lb = [WalletStat("0xW", realized_pnl=50_000, resolved_markets=0,
                     volume=500_000, win_rate=0.0)]
    closed = {"0xW": [100.0] * 5}  # only 5 resolved -> below the 30 floor
    client = MockPolymarketDataClient(leaderboard=lb, closed=closed)
    prov = PolymarketWhaleProvider(client, SmartWalletScorer(), {}, enrich=True)
    assert "0xW" not in prov._smart_addresses()
