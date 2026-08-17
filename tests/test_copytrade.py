from predictionedge.copytrade import (
    CopySignal,
    classify_market,
    copy_order_params,
    find_copy_signals,
    is_election_market,
    scan_smart_flow,
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
    # min_usd is the feed's CASH bar (size*price), as the live API applies it, so 5000
    # is what lets a 20,000-contract fill at 40c through.
    sigs = find_copy_signals(MockPolymarketDataClient(trades=trades), _scorer(),
                             min_usd=5000, max_price=0.90, now_ts=2000)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.market_id == "PMA" and s.outcome == "Yes"
    # 20000 contracts at 40c + 15000 at 45c = $14,750 of cash, NOT 35,000 of anything.
    assert s.n_wallets == 2 and abs(s.total_usd - 14_750) < 1e-6
    assert 0.41 < s.avg_price < 0.43          # contract-weighted ~0.421


def test_total_usd_is_cash_not_a_contract_count():
    """The units of `total_usd`, pinned. `Trade.size` off the feed is CONTRACTS.

    Priced at 25c so the two readings are 4x apart and cannot be mistaken for each
    other by a rounding tolerance: 40,000 contracts is $10,000 of risk. If this ever
    fails with the share total, the dollar figure has been swapped back for the count
    and every whale headline in the product is inflated by 1/price again.
    """
    trades = [_t("0xSHARP1", "PMA", "Yes", 40_000, price=0.25),
              _t("0xSHARP2", "PMA", "Yes", 8_000, price=0.25)]
    sigs, _ = _flow(trades, min_usd=1000)
    s = sigs[0]
    assert s.total_usd == 12_000, "total_usd must be size*price, not a share count"
    assert s.total_usd != 48_000, "total_usd is the sum of Trade.size - the units bug"
    # The per-wallet breakdown is fed to bankroll fractions, so it is money too.
    assert dict(s.wallet_usd) == {"0xSHARP1": 10_000, "0xSHARP2": 2_000}
    assert sum(u for _a, u in s.wallet_usd) == s.total_usd
    # The invariant that makes the units checkable from the outside: dollars divided by
    # the average price gives back the contracts, and the price stays a probability.
    assert abs(s.total_usd / s.avg_price - 48_000) < 1e-6
    assert 0.0 < s.avg_price <= 1.0
    # Same rule on the way out: an exit's total is what they took off the table.
    sells = [_t("0xSHARP1", "PMB", "Yes", 40_000, side="SELL", price=0.25)]
    _s2, exits = _flow(sells, min_usd=1000)
    assert exits[0].total_usd == 10_000 and exits[0].avg_price == 0.25


def test_signal_keeps_each_wallet_s_own_notional():
    """The total hides who committed what, and bankroll weighting is per wallet."""
    trades = [_t("0xSHARP1", "PMA", "Yes", 20000, price=0.40),
              _t("0xSHARP2", "PMA", "Yes", 15000, price=0.45),
              _t("0xSHARP1", "PMA", "Yes", 5000, price=0.41)]   # same wallet again
    sigs = find_copy_signals(MockPolymarketDataClient(trades=trades), _scorer(),
                             min_usd=1000, now_ts=2000)
    # Dollars per wallet: SHARP1 = 20000*0.40 + 5000*0.41, SHARP2 = 15000*0.45.
    assert dict(sigs[0].wallet_usd) == {"0xSHARP1": 10_050, "0xSHARP2": 6_750}
    assert sigs[0].n_wallets == 2


def test_min_wallets_gate():
    # 20,000 contracts at 40c = $8,000, comfortably over the feed bar, so the empty
    # result is the wallet gate doing its job and not the trade being filtered away.
    trades = [_t("0xSHARP1", "PMA", "Yes", 20000)]
    sigs = find_copy_signals(MockPolymarketDataClient(trades=trades), _scorer(),
                             min_usd=5000, min_wallets=2, now_ts=2000)
    assert sigs == []


def test_no_signals_when_no_smart_buys():
    trades = [_t("0xRANDO", "PMB", "No", 50000)]
    assert find_copy_signals(MockPolymarketDataClient(trades=trades), _scorer(),
                             min_usd=10000, now_ts=2000) == []


# --- E4.2 wallet exits --------------------------------------------------------
# The feed was read for BUYs only, which assumed informed wallets only ever express
# themselves by entering. A whale dumping is the same trader with the same information.

def _flow(trades, **kw):
    # $2k, the production default, and a bar these fixtures clear in CASH: the feed
    # filter is size*price on both the live client and the mock.
    kw.setdefault("min_usd", 2000)
    kw.setdefault("now_ts", 2000)
    kw.setdefault("track_exits", True)
    return scan_smart_flow(MockPolymarketDataClient(trades=trades), _scorer(), **kw)


def test_smart_sells_are_collected_not_discarded():
    trades = [_t("0xSHARP1", "PMA", "Yes", 20000, side="SELL", price=0.30, ts=1400),
              _t("0xSHARP2", "PMA", "Yes", 10000, side="SELL", price=0.32, ts=1700)]
    _sigs, exits = _flow(trades)
    assert len(exits) == 1
    x = exits[0]
    assert x.market_id == "PMA" and x.outcome == "Yes"
    assert x.n_wallets == 2 and x.total_usd == 9_200   # 20000*0.30 + 10000*0.32
    assert x.minutes_ago == 5.0            # from the LATEST sell (ts 1700), not the first


def test_exits_ignore_wallets_with_no_track_record():
    trades = [_t("0xRANDO", "PMA", "Yes", 50000, side="SELL")]
    assert _flow(trades)[1] == []


def test_exits_are_off_unless_asked_for():
    trades = [_t("0xSHARP1", "PMA", "Yes", 20000, side="SELL")]
    assert _flow(trades, track_exits=False)[1] == []


def test_sells_still_never_become_buy_signals():
    """The original guarantee: a sell must not read as smart money entering."""
    trades = [_t("0xSHARP1", "PMA", "Yes", 20000, side="SELL")]
    sigs, exits = _flow(trades)
    assert sigs == [] and len(exits) == 1


def test_buy_and_sell_on_one_market_are_kept_apart():
    trades = [_t("0xSHARP1", "PMA", "Yes", 20000, price=0.40, ts=1000),
              _t("0xSHARP2", "PMA", "Yes", 30000, side="SELL", price=0.44, ts=1800)]
    sigs, exits = _flow(trades)
    assert sigs[0].total_usd == 8_000        # the sell does not inflate the buy side
    assert exits[0].total_usd == 13_200      # 30000 contracts at 44c


# --- E4.3 fresh-wallet candidates ---------------------------------------------
# 0xRANDO is not on the leaderboard, which is the only state a genuinely new wallet
# can be in. These pin the SHAPE checks; age and depth are the board's job.

def test_unranked_wallet_becomes_a_fresh_candidate():
    trades = [_t("0xRANDO", "PMA", "Yes", 30000, price=0.40)]
    sigs, _ = _flow(trades, fresh_min_usd=5000)
    assert len(sigs) == 1 and sigs[0].fresh is True
    assert sigs[0].wallet_usd == (("0xRANDO", 12_000),)   # 30000 contracts at 40c


def test_fresh_candidates_are_off_by_default():
    """It must stay possible to run the board on proven wallets only."""
    trades = [_t("0xRANDO", "PMA", "Yes", 30000)]
    assert _flow(trades)[0] == []


def test_a_wallet_on_both_sides_is_hedging_not_betting():
    """One-sidedness is half the claim - a two-way wallet is a maker, not an opinion."""
    trades = [_t("0xRANDO", "PMA", "Yes", 30000, price=0.40),
              _t("0xRANDO", "PMA", "No", 30000, price=0.60)]
    assert _flow(trades, fresh_min_usd=5000)[0] == []


def test_small_unranked_trades_are_not_a_pattern():
    trades = [_t("0xRANDO", "PMA", "Yes", 12000, price=0.40)]   # $4,800 of cash
    assert _flow(trades, min_usd=1000, fresh_min_usd=25000)[0] == []


def test_the_fresh_wallet_bar_is_measured_in_dollars():
    """20,000 contracts is a $5k bet at 25c and a $12k bet at 60c - same count, and the
    threshold is named for money, so only the cash may decide."""
    cheap = [_t("0xRANDO", "PMA", "Yes", 20_000, price=0.25)]
    dear = [_t("0xRANDO", "PMA", "Yes", 20_000, price=0.60)]
    assert _flow(cheap, min_usd=1000, fresh_min_usd=10_000)[0] == []
    assert len(_flow(dear, min_usd=1000, fresh_min_usd=10_000)[0]) == 1


def test_fresh_candidates_do_not_pool_separate_wallets():
    """Two strangers on one side is not one concentrated bet - keep them apart."""
    trades = [_t("0xRANDO", "PMA", "Yes", 30000, price=0.40),
              _t("0xRANDO2", "PMA", "Yes", 30000, price=0.40)]
    sigs, _ = _flow(trades, fresh_min_usd=5000)
    assert len(sigs) == 2
    assert all(s.n_wallets == 1 for s in sigs)


def test_fresh_candidates_respect_the_near_resolution_cap():
    trades = [_t("0xRANDO", "PMA", "Yes", 30000, price=0.97)]
    assert _flow(trades, fresh_min_usd=5000, max_price=0.90)[0] == []


# --- E4.4 category-matched competence -----------------------------------------
# Every leaderboard was flattened into one "smart" pile, so a crypto specialist scored
# as an authority on a Senate race. These pin which board a market is asking about.

def test_classify_market_picks_the_right_leaderboard():
    assert classify_market("Lakers vs Celtics") == "SPORTS"
    assert classify_market("Will Trump win the 2028 presidential election") == "POLITICS"
    assert classify_market("Bitcoin above $120k on Friday") == "CRYPTO"


def test_crypto_wins_over_sports_wording():
    """'Bitcoin season high' is not a sporting event however many league words it has."""
    assert classify_market("Bitcoin above $120k this season") == "CRYPTO"


def test_unclassifiable_market_returns_empty_not_a_guess():
    """The fail-closed hinge: '' must mean 'no category rule', never a default board."""
    assert classify_market("Will it rain in Lagos on Tuesday") == ""
    assert classify_market("") == ""


def test_election_flag_is_narrower_than_politics():
    """A rate decision is political, but nobody's position in it is a claim on a race."""
    assert is_election_market("Who wins the 2028 presidential election") is True
    assert is_election_market("Will the Fed cut rates in September") is False


class _CatClient(MockPolymarketDataClient):
    """Different wallets top different boards - which the real API also reports."""

    def __init__(self, by_cat, **kw):
        super().__init__(**kw)
        self._by_cat = by_cat

    def leaderboard(self, category="OVERALL"):
        return list(self._by_cat.get(category, []))


def _stat(addr):
    from predictionedge.polymarket import WalletStat
    return WalletStat(addr, realized_pnl=120_000, resolved_markets=180,
                      volume=2_000_000, win_rate=0.61)


def test_signal_counts_only_wallets_proven_on_this_category():
    client = _CatClient({"SPORTS": [_stat("0xSPORT")], "CRYPTO": [_stat("0xCOIN")]},
                        trades=[_t("0xSPORT", "PMA", "Yes", 20000, title="Lakers vs Celtics"),
                                _t("0xCOIN", "PMA", "Yes", 20000, title="Lakers vs Celtics")])
    sigs = find_copy_signals(client, _scorer(), categories=("SPORTS", "CRYPTO"),
                             min_usd=1000, now_ts=2000)
    assert sigs[0].category == "SPORTS"
    assert sigs[0].n_wallets == 2 and sigs[0].n_category_matched == 1


def test_unknown_category_matches_nobody_rather_than_everybody():
    client = _CatClient({"SPORTS": [_stat("0xSPORT")]},
                        trades=[_t("0xSPORT", "PMA", "Yes", 20000, title="Something odd")])
    sigs = find_copy_signals(client, _scorer(), categories=("SPORTS",),
                             min_usd=1000, now_ts=2000)
    assert sigs[0].category == "" and sigs[0].n_category_matched == 0


def test_fresh_wallets_never_claim_category_competence():
    """No record anywhere means no record here - the category cannot rescue them."""
    trades = [_t("0xRANDO", "PMA", "Yes", 30000, title="Lakers vs Celtics")]
    sigs, _ = _flow(trades, fresh_min_usd=5000)
    assert sigs[0].fresh is True
    assert sigs[0].category == "SPORTS" and sigs[0].n_category_matched == 0


# Whale price 0.51, i.e. right next to the book below, so these fixtures exercise
# PRICING. The give-up bar is a separate question and gets its own tests.
_SIG_YES = CopySignal("cid", "T", "Yes", 2, 30000, 0.51, 5.0, slug="mkt")
_SIG_NO = CopySignal("cid", "T", "No", 2, 30000, 0.51, 5.0, slug="mkt")
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


def test_the_order_path_refuses_what_the_board_would_refuse():
    """It used to compare the copy to the whale's price not at all - no drift gate, no
    fee - so the armed half of the project would buy tickets the research half rejected
    by name. 4c past a 0.52 book is 4c of drift plus 1.75c of taker fee."""
    late = CopySignal("cid", "T", "Yes", 2, 30000, 0.48, 5.0, slug="mkt")
    assert copy_order_params(late, _MKT, size_usd=10) is None
    # And it is the SAME rule, not a second copy of it, so raising the budget past the
    # give-up admits it again.
    assert copy_order_params(late, _MKT, size_usd=10, max_give_up_c=6.0) is not None


def test_the_order_path_charges_the_fee_against_the_drift_budget():
    """A 3c drift fits a 4c budget until the fee is charged to the same budget."""
    drifted = CopySignal("cid", "T", "Yes", 2, 30000, 0.49, 5.0, slug="mkt")
    assert copy_order_params(drifted, _MKT, size_usd=10) is None
    # Fee-blind, this is the ticket that used to go through: 3c of drift, 4c allowed.
    assert copy_order_params(drifted, _MKT, size_usd=10, fee_multiplier=0.0) is not None


# --- esports classifier -------------------------------------------------------

def test_is_esports_matches_game_names_and_the_best_of_convention():
    from predictionedge.copytrade import is_esports
    assert is_esports("Counter-Strike: BIG vs G2 (BO1) - Esports World Cup Group A")
    assert is_esports("LoL: KT Rolster vs Dplus KIA (BO3) - LCK Round 3-4")
    assert is_esports("Valorant: Evil Geniuses GC vs Arashi GC (BO3)")
    assert is_esports("Game Handicap: NS (-1.5) vs DN SOOPers (+1.5)",
                      "https://polymarket.com/event/lol-dnf-ns-2026-08-12")


def test_is_esports_does_not_swallow_ordinary_sports():
    """League acronyms are deliberately not matched: `lec-tig-vwh` is a Leagues Cup
    soccer fixture, and treating it as esports would cut real sports rows."""
    from predictionedge.copytrade import is_esports
    assert not is_esports("Will Vancouver Whitecaps FC win on 2026-08-11?",
                          "https://polymarket.com/event/lec-tig-vwh-2026-08-11")
    assert not is_esports("New York Mets vs. Pittsburgh Pirates",
                          "https://polymarket.com/event/mlb-nym-pit-2026-08-07")
    assert not is_esports("Cincinnati Open: Cameron Norrie vs Dino Prizmic",
                          "https://polymarket.com/event/atp-nor-priz-2026-08-13")
