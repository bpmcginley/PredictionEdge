"""Runtime configuration, loaded from environment / .env.

Nothing here is required to run in --mock mode. Live read-only mode needs the
Kalshi + odds keys; live *trading* additionally requires PE_LIVE_TRADING=1 (a
deliberate, explicit opt-in - the default is dry-run even with real credentials).
See .env.example.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def _load_dotenv() -> None:
    """Best-effort .env loader (no hard dependency on python-dotenv)."""
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
        return
    except Exception:
        pass
    env = Path(".env")
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    # --- Bankroll & per-trade sizing (tuned for a sub-$1k book) ---
    bankroll: float = 500.0
    kelly_fraction: float = 0.25           # fractional Kelly - never full
    per_market_max_fraction: float = 0.05  # cap any single market at 5% of bankroll
    min_edge: float = 0.03                 # require >=3c net edge per contract to act
    max_contracts: int = 200

    # --- Fees ---
    fee_multiplier: float = 0.07
    assume_maker: bool = True              # we post resting orders, so model maker fees

    # --- De-vig ---
    # "power", not "multiplicative": the literature (Štrumbelj 2014 + practitioner
    # replications) is consistent that multiplicative is the LEAST accurate de-vig,
    # with power/Shin best recovering true probabilities on lopsided books. Every
    # sports record still carries fair_mult/fair_power/fair_shin so the paper trial
    # can adjudicate the choice from one run. Also valid: "shin", "blend"
    # (power+Shin averaged in log-odds), "multiplicative", "additive".
    devig_method: str = "power"

    # --- Market matching ---
    dynamic_discovery: bool = False        # auto-discover & match Kalshi sports markets
    max_scan_markets: int = 60             # cap discovered markets/scan (bounds API calls)
    # Verified Kalshi moneyline series (per-GAME, not the bare futures series):
    sports_series: tuple[str, ...] = (
        "KXNBAGAME", "KXNFLGAME", "KXMLBGAME", "KXNHLGAME", "KXEPLGAME",
    )
    # The Odds API sport keys to pull consensus from (in-season majors):
    odds_sports: tuple[str, ...] = (
        "americanfootball_nfl", "baseball_mlb", "icehockey_nhl",
        "basketball_nba", "soccer_epl",
    )
    # Crypto statistical edge (lognormal spot+vol model; free data, no Odds API):
    crypto_enabled: bool = True
    # PAPER-only until validated: live inspection shows the naive realized-vol model
    # systematically disagrees with liquid crypto markets (likely model error, not edge),
    # so crypto trades are recorded for backtest but NOT placed live until proven.
    crypto_live: bool = False
    # SHORT-TERM daily point-price series (not the year-out EOY markets):
    crypto_series: tuple[str, ...] = ("KXBTCD", "KXETHD", "KXSOLD", "KXXRPD")
    # MEASURED 2026-08-07, so nobody widens this again expecting it to help: every open
    # market in all four series resolved in 0.03 or 0.20 DAYS - there were exactly two
    # expiries and nothing beyond a day. The 10-day cap is not binding and raising it
    # admits zero extra markets (`resolves beyond 10d` counts 0 in the scan report).
    # The binding filter is crypto_min_hours below, which drops the 43-minute cycle.
    # Two consequences worth knowing before touching either number:
    #  - Deribit's FRONT expiry is ~0.66 days out, so these markets all fall before it.
    #    deribit.py refuses to extrapolate in time, which is correct, so BTC/ETH here
    #    are structurally unpriceable by that route no matter what the window says.
    #  - Kalshi's longer-dated crypto series (KXBTCMAXM, BTCMINMAXY, KXETHMINMON, ...)
    #    are MAX/MIN/one-touch markets. Those are path-dependent barriers; a terminal
    #    risk-neutral density is the wrong object for them. Do not add them here just
    #    because their horizon suits Deribit.
    crypto_max_days: float = 10.0    # skip long-dated markets (no edge a week+ out)
    crypto_min_hours: float = 1.0    # skip near-instant markets (model unreliable/scalpy)

    # Copy-trading: mirror profitable Polymarket wallets (execute on Polymarket US):
    copytrade_enabled: bool = True
    # Min trade size to count as a whale buy. Tuned WITH copytrade_feed_limit: the two
    # interact through the time window. A $10k floor over 1500 trades reaches ~45h back,
    # so nearly everything fails staleness; $2k fills the same window with fresher flow.
    # Measured 2026-08-05: $10k -> 0 tickets, $2k -> 8, same filters.
    copytrade_min_usd: float = 2000.0
    copytrade_min_wallets: int = 1       # how many profitable wallets must agree
    copytrade_max_price: float = 0.90    # skip near-resolved markets (no upside)
    copytrade_size_usd: float = 5.0      # $ per copy order (small; scale after validation)
    copytrade_categories: tuple[str, ...] = ("OVERALL", "SPORTS", "POLITICS", "CRYPTO")
    # How far back the trade feed reaches. This - not min_usd - is what caps how many
    # signals exist: 500 trades is only ~20h, while staleness downstream allows 48h.
    copytrade_feed_limit: int = 1500
    # ALL-time boards are dominated by wallets that got rich years ago; MONTH/WEEK
    # surface who is informed right now. Union of both is a deeper, fresher universe.
    copytrade_time_periods: tuple[str, ...] = ("ALL", "MONTH", "WEEK")
    copytrade_leaderboard_limit: int = 100

    # --- Whale (supporting) signal ---
    whale_weight: float = 0.0              # 0 until validated on real on-chain data
    whale_source: str = "none"             # "none" | "polymarket"
    whale_map_path: str = "data/whale_map.json"  # kalshi_ticker -> polymarket market id
    whale_min_resolved: int = 30           # min resolved markets for a "smart" wallet
    whale_min_pnl: float = 5000.0          # min realised PnL for a "smart" wallet
    whale_max_wallets: int = 50
    whale_enrich: bool = True              # fetch per-wallet win-rate/sample (stronger filter)
    # Whale-as-PRIMARY signal (politics/econ/news markets, where insider edge exists -
    # there's no sportsbook to de-vig, so smart-money positioning IS the fair value):
    whale_primary: bool = True
    whale_primary_min_conf: float = 0.30   # min signal confidence to act on it as fair value

    # --- SIGNAL BOARD (manual trading) ---------------------------------------
    # Master switch for the pivot: the bot researches and ranks, a human places any
    # trade. While this is True every execution path is refused - see executor.py.
    # Default True: advice is the safe default.
    research_only: bool = True
    board_account_size: float = 100_000.0    # capital the board sizes against (paper)
    board_per_trade_fraction: float = 0.01   # our risk budget per idea
    # Skip anything resolving sooner than this. Deliberately short: for same-day sports
    # the informed money arrives LATE (lineups, injuries, weather), so a whale betting
    # 3h out knows more than one betting 3 days out. The June-26 disaster was in-play
    # copying, which the separate game_start filter blocks - this one never did that job.
    board_min_hours: float = 3.0
    board_max_days: float = 30.0            # and skip the far future - no edge a year out
    # Staleness scales with the market's own horizon: a 9h-old position on something
    # resolving in two days is still information; the same age on a match in progress
    # is worthless. Allowed age = 20% of time-to-resolution, floored and capped below.
    board_max_signal_age_min: float = 120.0      # floor
    board_stale_fraction: float = 0.20
    board_max_signal_age_cap_min: float = 2880.0  # 48h ceiling
    board_min_price: float = 0.05
    board_max_price: float = 0.90
    board_max_drift_c: float = 4.0          # max cents price may run past the whale's entry
    # THE BUY BAR. A ticket is bought because it is good enough, not because it placed
    # in the day's top N - a quota buys the ninth-best ticket on a quiet day and skips a
    # better one on a busy day, which is backwards.
    #
    # READ THIS BEFORE MOVING IT. Conviction is a hand-weighted blend (agreement .30,
    # size .25, drift .20, recency .15, time-to-resolve .10) that sums to 1.0 because
    # the weights were chosen to, not because anything was fitted to an outcome. It is
    # NOT a probability and 0.45 does not mean 45% anything. No number here is defensible
    # from evidence yet; this one is a placeholder held at roughly the old top-8 cutline
    # (the 2026-08-07 board ran 0.486-0.603 for its published eight) so behaviour is
    # about what it was, and it should be REPLACED by whatever the paper trial's settled
    # rows say once there are enough of them to say anything.
    board_min_conviction: float = 0.45
    # Everything from here up to the buy bar is recorded by the paper trial but never
    # recommended. Without observing the rejected region the bar can never be checked -
    # you would only ever see outcomes above your own cutoff, which is the textbook way
    # to hold a threshold forever on no evidence.
    board_probe_min_conviction: float = 0.25
    # 0 = no cap. An opt-in valve for capping exposure on an unusually loud day, not the
    # gate: it used to truncate the ranked list silently and hide half the flow.
    board_max_tickets: int = 0
    journal_path: str = "data/manual_journal.jsonl"

    # --- EDGE EXPANSION (see EDGE_PLAN.md) -----------------------------------
    # E1 - logical-consistency scanner. Ladder monotonicity, bracket sums and
    # dominance violations are edges that need no forecast to be right. A violation
    # smaller than the round-trip spread is not an edge, hence the cents floor.
    consistency_enabled: bool = True
    consistency_min_edge_c: float = 2.0     # min net-of-spread violation to report
    consistency_max_events: int = 400       # bound the Gamma crawl per scan
    consistency_min_liquidity: float = 5_000.0   # both legs must be actually fillable

    # E2 - crypto risk-neutral density from Deribit's option chain. The realized-vol
    # lognormal model disagrees with liquid markets by ~7c, which is model error, not
    # edge: realized vol is not the risk-neutral distribution. BTC/ETH only - Deribit
    # has no meaningful SOL/XRP chain, and those must stay labelled as lognormal.
    deribit_enabled: bool = True
    deribit_base_url: str = "https://www.deribit.com/api/v2"
    deribit_assets: tuple[str, ...] = ("BTC", "ETH")
    deribit_max_spread_frac: float = 0.25   # discard option quotes wider than this
    deribit_min_strikes: int = 8            # too few strikes = fail closed, don't guess

    # E3 - macro fair value. Fed funds futures are the reference for Fed markets; the
    # Cleveland Fed nowcast is the reference for CPI. Both free, both better than retail.
    macro_enabled: bool = True
    # A nowcast is a point estimate; turning it into bracket probabilities needs an
    # explicit uncertainty. Named constant, not a magic number inside a formula.
    # MEASURED, not guessed: the Cleveland Fed's own final-nowcast-vs-actual error across
    # all 154 printed vintages is rmse 0.150 (CPI MoM) to 0.162 (Core YoY), essentially
    # unbiased. The initial 0.12 guess was too tight, and a too-tight sigma INVENTS edge -
    # it makes our distribution narrower than reality, so every centre bracket reads
    # "overpriced". macrofv floors this at 0.16 and says so when it widens.
    macro_cpi_nowcast_sigma: float = 0.16   # pp std-dev of nowcast vs printed CPI
    macro_max_gap_c: float = 25.0           # gaps beyond this smell like a mismatch, not edge

    # E4 - whale depth. Position size as a share of the wallet's own bankroll beats raw
    # dollars: $50k from a $10M wallet is noise, from a $100k wallet it is an opinion.
    whale_bankroll_weighting: bool = True
    whale_track_exits: bool = True          # a smart wallet selling is information too
    whale_fresh_enabled: bool = True        # new wallet + big one-sided bet = informed flow
    whale_fresh_max_age_days: float = 30.0
    whale_fresh_min_usd: float = 5_000.0
    whale_fresh_max_liquidity: float = 250_000.0  # the pattern lives in thin markets
    whale_category_match: bool = True       # a politics whale is not a soccer whale
    # Election markets have a documented single-whale distortion problem. There,
    # concentration is a reason for LESS confidence in the price - the inverse of how
    # concentrated smart money is scored everywhere else.
    whale_election_concentration_penalty: bool = True

    # --- AUTONOMOUS OPERATION / SAFETY ---------------------------------------
    # Master switch. Real orders are placed ONLY when this is explicitly true.
    live_trading: bool = False
    use_demo: bool = True                  # when live, hit Kalshi's demo env first
    # Account-level circuit breakers (the bot runs unattended - these are the brakes)
    max_daily_loss: float = 25.0           # halt for the day after this realised loss ($)
    max_total_exposure: float = 200.0      # cap aggregate open stake across markets ($)
    max_open_positions: int = 8
    max_orders_per_cycle: int = 3          # throttle how fast it can build a book
    sane_edge_ceiling: float = 0.25        # reject "too good to be true" edges (data error)
    min_price: float = 0.05                # skip illiquid extreme tails
    max_price: float = 0.95
    # Order review / arbitrage hedging (see arbitrage.py)
    arb_enabled: bool = True
    arb_min_ratio: float = 0.02            # min r_max (slack/capital) to place a hedge
    stale_cancel_band: float = 0.08        # cancel an unfilled maker order if prob moved this far against it
    require_demo_first: bool = True         # block live prod until a demo run is logged
    # Operational files / cadence
    killswitch_path: str = "data/STOP"      # presence of this file halts all trading
    control_path: str = "data/control.json"  # dashboard arm switch (live on/off)
    heartbeat_path: str = "data/heartbeat.json"
    state_db_path: str = "data/state.db"
    log_path: str = "data/predictionedge.log"
    poll_interval: float = 300.0            # seconds between cycles in daemon mode

    # --- Credentials ---
    kalshi_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    demo_base_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    odds_api_key: str = ""

    # --- Venue bridge (papertrial): re-record whale tickets at PM-US/Kalshi asks ---
    bridge_enabled: bool = True            # public quote reads only; no keys, no orders
    bridge_kalshi_series: str = ""         # extra league:series pairs, comma-separated

    # --- Polymarket US (QCX) venue (Ed25519 auth) ---
    pmus_enabled: bool = False
    pmus_key_id: str = ""
    pmus_private_key_path: str = ""        # Ed25519 PEM
    pmus_base_url: str = "https://clob.polymarket.us"   # VERIFY

    # --- NWS weather markets on Kalshi (RETIRED 2026-08-15) -------------------
    # OFF because the sleeve's thesis was measured and is FALSE. It claimed "the NWS
    # locates tomorrow's high better than the Kalshi ladder does". Scored against the
    # unbiased sample in docs/wxlog.json (every city-day looked at, not just the ones
    # bet), the ladder's own implied mean beat the NBM forecast at EVERY lead bucket:
    #   contemporaneous  NBM 1.77F vs ladder 0.45F, NBM closer on  1/15 city-days
    #   first sighting   NBM 1.75F vs ladder 1.15F, NBM closer on  3/14 (sign p~0.03)
    #   >24h lead only   NBM 1.80F vs ladder 1.11F, NBM closer on  2/10
    # Three candidate fixes were tested and all fail:
    #   - per-city bias correction (NY runs +2.5F warm, Miami -1.9F cold, both with
    #     consistent sign) closes only a third of the gap: 1.77F -> 1.43F vs 0.45F;
    #   - restricting to the unbiased cities does not help - Chicago's NBM still loses
    #     to the ladder on 3 of 4 days and went 0-for-3 on tickets;
    #   - raising `weather_min_edge` makes it WORSE, not better. The edge estimate is
    #     computed from a mislocated mean, so a higher bar selects harder for the days
    #     the model is most wrong. That is adverse selection, not a filter.
    # The structural reason, which is why no tweak rescues it: the ladder already
    # prices this same free public NBM bulletin, and then keeps updating on intraday
    # observations the bulletin lags. There is no informational asymmetry to harvest.
    # Realised while live: 1 win in 10 settled tickets, -$884, t=-4.95.
    # `weatherlog` still runs every cycle and is deliberately NOT gated on this flag -
    # it costs one public call, it is the evidence trail, and it is the only thing that
    # could re-open the case. Re-arm with PE_WEATHER_ENABLED=1 if it ever does.
    weather_enabled: bool = False
    weather_min_edge: float = 0.07
    weather_max_days: float = 3.0

    # --- Calibration overlay (calibration.py) --------------------------------
    # Sanity layer from Le 2026 (arXiv 2602.19520): correct the market's measured
    # domain x horizon miscalibration out of its price and suppress any ticket whose
    # edge does not survive the correction. Shrink applies only that fraction of the
    # published slopes (the study window ended Dec 2025 and slopes drift); 0 disables
    # the correction entirely, 1 applies it in full.
    calibration_overlay: bool = True
    calibration_shrink: float = 0.5

    # --- Maker-first paper entries (papertrial) ---
    # Most edges this system trades are 1-3c gross while the taker fee peaks at
    # 1.75c/contract, so passive entry vs crossing the spread is the single biggest
    # EV lever - the trial must simulate it honestly rather than assume it. When on,
    # a ticket that carries a bid becomes a PENDING order at that bid instead of an
    # instant taker fill at the ask; it fills only if a later poll's ask comes to it.
    maker_first: bool = True
    # ~2 hours at the 15-minute board cadence. An order still unfilled then either
    # cancels (default - the honest accounting: the trade never happened) or, with
    # the fallback flag, crosses the spread at the then-current ask at taker fees.
    maker_max_polls: int = 8
    maker_fallback_taker: bool = False

    # --- AAA national gas price markets on Kalshi (paper-only research sleeve) ---
    # The weather sleeve's architecture pointed at gasprices.aaa.com (see gas.py).
    # `gas_min_edge` sits ON TOP of the taker fee, which gas.py charges explicitly.
    gas_enabled: bool = True
    gas_min_edge: float = 0.05
    gas_max_days: float = 2.0
    gas_ewma_window: int = 5        # span of the EWMA drift over daily deltas
    gas_sigma_window: int = 10      # how many recent deltas the sigma is fit on
    gas_sigma_floor: float = 0.0015  # dollars (0.15c): sparse history can't imply certainty
    gas_min_deltas: int = 3         # observed daily moves required before any ticket

    # --- SPX/NDX 0DTE options-implied density vs Kalshi index ladders (paper) ---
    # Reads CBOE's free 15-min-delayed chain; every ticket is paper-only by
    # construction (source "spxindex", venue "kalshi", nothing in the order path).
    spx_enabled: bool = True
    spx_min_edge: float = 0.03             # required edge BEYOND the taker fee
    # Risk-neutral tails are fatter than real-world ones (the variance risk
    # premium). Implied sigma is multiplied by this before bracket probabilities
    # are computed, and the value used is recorded on every ticket so it can be
    # re-fit from settled rows instead of argued about.
    spx_vrp_shrink: float = 0.92
    # The chain is 15 minutes delayed; near the close that delay is toxic. No
    # tickets at or after this ET wall-clock time ("HH:MM", 24h).
    spx_blackout_start_et: str = "15:00"
    spx_min_strikes: int = 8               # thinner smile than this = fail closed
    spx_max_spread_frac: float = 0.25      # drop option quotes wider than this
    # Where the density's location comes from. "chain" = the payload's own delayed
    # spot (same-timestamp internal consistency). A future fresher source gets a
    # new name here rather than silently changing what spot_used means.
    spx_spot_source: str = "chain"

    # --- Paper trading / backtest dataset ---
    paper_ledger_path: str = "data/paper_ledger.jsonl"
    settled_path: str = "data/settled_bets.jsonl"   # labeled bets for the backtest

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv()
        e = os.environ

        def f(name: str, default: float) -> float:
            v = e.get(name)
            return float(v) if v else default

        return cls(
            bankroll=f("PE_BANKROLL", cls.bankroll),
            kelly_fraction=f("PE_KELLY_FRACTION", cls.kelly_fraction),
            per_market_max_fraction=f("PE_PER_MARKET_MAX_FRACTION", cls.per_market_max_fraction),
            min_edge=f("PE_MIN_EDGE", cls.min_edge),
            max_contracts=int(f("PE_MAX_CONTRACTS", cls.max_contracts)),
            fee_multiplier=f("PE_FEE_MULTIPLIER", cls.fee_multiplier),
            assume_maker=_truthy(e.get("PE_ASSUME_MAKER"), cls.assume_maker),
            devig_method=e.get("PE_DEVIG_METHOD", cls.devig_method),
            dynamic_discovery=_truthy(e.get("PE_DYNAMIC_DISCOVERY"), cls.dynamic_discovery),
            max_scan_markets=int(f("PE_MAX_SCAN_MARKETS", cls.max_scan_markets)),
            sports_series=tuple(s.strip() for s in e.get("PE_SPORTS_SERIES", "").split(",")
                                if s.strip()) or cls.sports_series,
            odds_sports=tuple(s.strip() for s in e.get("PE_ODDS_SPORTS", "").split(",")
                              if s.strip()) or cls.odds_sports,
            crypto_enabled=_truthy(e.get("PE_CRYPTO_ENABLED"), cls.crypto_enabled),
            crypto_live=_truthy(e.get("PE_CRYPTO_LIVE"), cls.crypto_live),
            crypto_series=tuple(s.strip() for s in e.get("PE_CRYPTO_SERIES", "").split(",")
                                if s.strip()) or cls.crypto_series,
            crypto_max_days=f("PE_CRYPTO_MAX_DAYS", cls.crypto_max_days),
            crypto_min_hours=f("PE_CRYPTO_MIN_HOURS", cls.crypto_min_hours),
            copytrade_enabled=_truthy(e.get("PE_COPYTRADE_ENABLED"), cls.copytrade_enabled),
            copytrade_min_usd=f("PE_COPYTRADE_MIN_USD", cls.copytrade_min_usd),
            copytrade_min_wallets=int(f("PE_COPYTRADE_MIN_WALLETS", cls.copytrade_min_wallets)),
            copytrade_max_price=f("PE_COPYTRADE_MAX_PRICE", cls.copytrade_max_price),
            copytrade_size_usd=f("PE_COPYTRADE_SIZE_USD", cls.copytrade_size_usd),
            copytrade_categories=tuple(s.strip() for s in
                                       e.get("PE_COPYTRADE_CATEGORIES", "").split(",")
                                       if s.strip()) or cls.copytrade_categories,
            copytrade_feed_limit=int(f("PE_COPYTRADE_FEED_LIMIT", cls.copytrade_feed_limit)),
            copytrade_time_periods=tuple(s.strip() for s in
                                         e.get("PE_COPYTRADE_TIME_PERIODS", "").split(",")
                                         if s.strip()) or cls.copytrade_time_periods,
            copytrade_leaderboard_limit=int(f("PE_COPYTRADE_LEADERBOARD_LIMIT",
                                              cls.copytrade_leaderboard_limit)),
            whale_weight=f("PE_WHALE_WEIGHT", cls.whale_weight),
            whale_source=e.get("PE_WHALE_SOURCE", cls.whale_source),
            whale_map_path=e.get("PE_WHALE_MAP", cls.whale_map_path),
            whale_min_resolved=int(f("PE_WHALE_MIN_RESOLVED", cls.whale_min_resolved)),
            whale_min_pnl=f("PE_WHALE_MIN_PNL", cls.whale_min_pnl),
            whale_max_wallets=int(f("PE_WHALE_MAX_WALLETS", cls.whale_max_wallets)),
            whale_enrich=_truthy(e.get("PE_WHALE_ENRICH"), cls.whale_enrich),
            whale_primary=_truthy(e.get("PE_WHALE_PRIMARY"), cls.whale_primary),
            whale_primary_min_conf=f("PE_WHALE_PRIMARY_MIN_CONF", cls.whale_primary_min_conf),
            research_only=_truthy(e.get("PE_RESEARCH_ONLY"), cls.research_only),
            # PE_OMEN_* are the pre-rename env names, honored so an old deployment
            # keeps its configured sizing after upgrading.
            board_account_size=f("PE_BOARD_ACCOUNT_SIZE",
                                 f("PE_OMEN_ACCOUNT_SIZE", cls.board_account_size)),
            board_per_trade_fraction=f("PE_BOARD_PER_TRADE_FRACTION",
                                       f("PE_OMEN_PER_TRADE_FRACTION",
                                         cls.board_per_trade_fraction)),
            board_min_hours=f("PE_BOARD_MIN_HOURS", cls.board_min_hours),
            board_max_days=f("PE_BOARD_MAX_DAYS", cls.board_max_days),
            board_max_signal_age_min=f("PE_BOARD_MAX_SIGNAL_AGE_MIN",
                                       cls.board_max_signal_age_min),
            board_stale_fraction=f("PE_BOARD_STALE_FRACTION", cls.board_stale_fraction),
            board_max_signal_age_cap_min=f("PE_BOARD_MAX_SIGNAL_AGE_CAP_MIN",
                                           cls.board_max_signal_age_cap_min),
            board_min_price=f("PE_BOARD_MIN_PRICE", cls.board_min_price),
            board_max_price=f("PE_BOARD_MAX_PRICE", cls.board_max_price),
            board_max_drift_c=f("PE_BOARD_MAX_DRIFT_C", cls.board_max_drift_c),
            board_min_conviction=f("PE_BOARD_MIN_CONVICTION", cls.board_min_conviction),
            board_probe_min_conviction=f("PE_BOARD_PROBE_MIN_CONVICTION",
                                         cls.board_probe_min_conviction),
            board_max_tickets=int(f("PE_BOARD_MAX_TICKETS", cls.board_max_tickets)),
            journal_path=e.get("PE_JOURNAL_PATH", cls.journal_path),
            consistency_enabled=_truthy(e.get("PE_CONSISTENCY_ENABLED"), cls.consistency_enabled),
            consistency_min_edge_c=f("PE_CONSISTENCY_MIN_EDGE_C", cls.consistency_min_edge_c),
            consistency_max_events=int(f("PE_CONSISTENCY_MAX_EVENTS", cls.consistency_max_events)),
            consistency_min_liquidity=f("PE_CONSISTENCY_MIN_LIQUIDITY",
                                        cls.consistency_min_liquidity),
            deribit_enabled=_truthy(e.get("PE_DERIBIT_ENABLED"), cls.deribit_enabled),
            deribit_base_url=e.get("PE_DERIBIT_BASE_URL", cls.deribit_base_url),
            deribit_assets=tuple(s.strip().upper() for s in
                                 e.get("PE_DERIBIT_ASSETS", "").split(",")
                                 if s.strip()) or cls.deribit_assets,
            deribit_max_spread_frac=f("PE_DERIBIT_MAX_SPREAD_FRAC", cls.deribit_max_spread_frac),
            deribit_min_strikes=int(f("PE_DERIBIT_MIN_STRIKES", cls.deribit_min_strikes)),
            macro_enabled=_truthy(e.get("PE_MACRO_ENABLED"), cls.macro_enabled),
            macro_cpi_nowcast_sigma=f("PE_MACRO_CPI_NOWCAST_SIGMA", cls.macro_cpi_nowcast_sigma),
            macro_max_gap_c=f("PE_MACRO_MAX_GAP_C", cls.macro_max_gap_c),
            whale_bankroll_weighting=_truthy(e.get("PE_WHALE_BANKROLL_WEIGHTING"),
                                             cls.whale_bankroll_weighting),
            whale_track_exits=_truthy(e.get("PE_WHALE_TRACK_EXITS"), cls.whale_track_exits),
            whale_fresh_enabled=_truthy(e.get("PE_WHALE_FRESH_ENABLED"), cls.whale_fresh_enabled),
            whale_fresh_max_age_days=f("PE_WHALE_FRESH_MAX_AGE_DAYS",
                                       cls.whale_fresh_max_age_days),
            whale_fresh_min_usd=f("PE_WHALE_FRESH_MIN_USD", cls.whale_fresh_min_usd),
            whale_fresh_max_liquidity=f("PE_WHALE_FRESH_MAX_LIQUIDITY",
                                        cls.whale_fresh_max_liquidity),
            whale_category_match=_truthy(e.get("PE_WHALE_CATEGORY_MATCH"),
                                         cls.whale_category_match),
            whale_election_concentration_penalty=_truthy(
                e.get("PE_WHALE_ELECTION_CONCENTRATION_PENALTY"),
                cls.whale_election_concentration_penalty),
            live_trading=_truthy(e.get("PE_LIVE_TRADING"), cls.live_trading),
            use_demo=_truthy(e.get("PE_USE_DEMO"), cls.use_demo),
            max_daily_loss=f("PE_MAX_DAILY_LOSS", cls.max_daily_loss),
            max_total_exposure=f("PE_MAX_TOTAL_EXPOSURE", cls.max_total_exposure),
            max_open_positions=int(f("PE_MAX_OPEN_POSITIONS", cls.max_open_positions)),
            max_orders_per_cycle=int(f("PE_MAX_ORDERS_PER_CYCLE", cls.max_orders_per_cycle)),
            sane_edge_ceiling=f("PE_SANE_EDGE_CEILING", cls.sane_edge_ceiling),
            min_price=f("PE_MIN_PRICE", cls.min_price),
            max_price=f("PE_MAX_PRICE", cls.max_price),
            arb_enabled=_truthy(e.get("PE_ARB_ENABLED"), cls.arb_enabled),
            arb_min_ratio=f("PE_ARB_MIN_RATIO", cls.arb_min_ratio),
            stale_cancel_band=f("PE_STALE_CANCEL_BAND", cls.stale_cancel_band),
            require_demo_first=_truthy(e.get("PE_REQUIRE_DEMO_FIRST"), cls.require_demo_first),
            killswitch_path=e.get("PE_KILLSWITCH", cls.killswitch_path),
            control_path=e.get("PE_CONTROL_PATH", cls.control_path),
            heartbeat_path=e.get("PE_HEARTBEAT", cls.heartbeat_path),
            state_db_path=e.get("PE_STATE_DB", cls.state_db_path),
            log_path=e.get("PE_LOG_PATH", cls.log_path),
            poll_interval=f("PE_POLL_INTERVAL", cls.poll_interval),
            kalshi_key_id=e.get("KALSHI_API_KEY_ID", ""),
            kalshi_private_key_path=e.get("KALSHI_PRIVATE_KEY_PATH", ""),
            kalshi_base_url=e.get("KALSHI_BASE_URL", cls.kalshi_base_url),
            demo_base_url=e.get("KALSHI_DEMO_BASE_URL", cls.demo_base_url),
            odds_api_key=e.get("ODDS_API_KEY", ""),
            bridge_enabled=_truthy(e.get("PE_BRIDGE_ENABLED"), cls.bridge_enabled),
            bridge_kalshi_series=e.get("PE_BRIDGE_KALSHI_SERIES", ""),
            pmus_enabled=_truthy(e.get("PE_PMUS_ENABLED"), cls.pmus_enabled),
            pmus_key_id=e.get("PMUS_API_KEY_ID", ""),
            pmus_private_key_path=e.get("PMUS_PRIVATE_KEY_PATH", ""),
            pmus_base_url=e.get("PMUS_BASE_URL", cls.pmus_base_url),
            weather_enabled=_truthy(e.get("PE_WEATHER_ENABLED"), cls.weather_enabled),
            weather_min_edge=f("PE_WEATHER_MIN_EDGE", cls.weather_min_edge),
            weather_max_days=f("PE_WEATHER_MAX_DAYS", cls.weather_max_days),
            calibration_overlay=_truthy(e.get("PE_CALIBRATION_OVERLAY"),
                                        cls.calibration_overlay),
            calibration_shrink=f("PE_CALIBRATION_SHRINK", cls.calibration_shrink),
            maker_first=_truthy(e.get("PE_MAKER_FIRST"), cls.maker_first),
            maker_max_polls=int(f("PE_MAKER_MAX_POLLS", cls.maker_max_polls)),
            maker_fallback_taker=_truthy(e.get("PE_MAKER_FALLBACK_TAKER"),
                                         cls.maker_fallback_taker),
            gas_enabled=_truthy(e.get("PE_GAS_ENABLED"), cls.gas_enabled),
            gas_min_edge=f("PE_GAS_MIN_EDGE", cls.gas_min_edge),
            gas_max_days=f("PE_GAS_MAX_DAYS", cls.gas_max_days),
            gas_ewma_window=int(f("PE_GAS_EWMA_WINDOW", cls.gas_ewma_window)),
            gas_sigma_window=int(f("PE_GAS_SIGMA_WINDOW", cls.gas_sigma_window)),
            gas_sigma_floor=f("PE_GAS_SIGMA_FLOOR", cls.gas_sigma_floor),
            gas_min_deltas=int(f("PE_GAS_MIN_DELTAS", cls.gas_min_deltas)),
            spx_enabled=_truthy(e.get("PE_SPX_ENABLED"), cls.spx_enabled),
            spx_min_edge=f("PE_SPX_MIN_EDGE", cls.spx_min_edge),
            spx_vrp_shrink=f("PE_SPX_VRP_SHRINK", cls.spx_vrp_shrink),
            spx_blackout_start_et=e.get("PE_SPX_BLACKOUT_START_ET",
                                        cls.spx_blackout_start_et),
            spx_min_strikes=int(f("PE_SPX_MIN_STRIKES", cls.spx_min_strikes)),
            spx_max_spread_frac=f("PE_SPX_MAX_SPREAD_FRAC", cls.spx_max_spread_frac),
            spx_spot_source=e.get("PE_SPX_SPOT_SOURCE", cls.spx_spot_source),
            paper_ledger_path=e.get("PE_PAPER_LEDGER", cls.paper_ledger_path),
            settled_path=e.get("PE_SETTLED_PATH", cls.settled_path),
        )

    @property
    def has_live_credentials(self) -> bool:
        return bool(self.kalshi_key_id and self.kalshi_private_key_path and self.odds_api_key)

    @property
    def active_base_url(self) -> str:
        """The Kalshi base URL trading will actually hit (demo unless explicitly prod)."""
        return self.demo_base_url if self.use_demo else self.kalshi_base_url
