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
    devig_method: str = "multiplicative"

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

    # --- OMEN SIGNAL BOARD (manual trading) ----------------------------------
    # Master switch for the pivot: the bot researches and ranks, a human places the
    # trade on Omen. Omen forbids automation of any kind (no API, no bots, no browser
    # automation, no signal-following services), so while this is True every execution
    # path is refused - see executor.py. Default True: advice is the safe default.
    research_only: bool = True
    omen_account_size: float = 100_000.0    # simulated capital of the Omen account
    omen_per_trade_fraction: float = 0.01   # our risk budget per idea (not an Omen rule)
    board_min_hours: float = 24.0           # skip anything resolving sooner (in-play trap)
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
    board_min_conviction: float = 0.35
    board_max_tickets: int = 8
    journal_path: str = "data/manual_journal.jsonl"

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

    # --- Polymarket US (QCX) venue (Ed25519 auth) ---
    pmus_enabled: bool = False
    pmus_key_id: str = ""
    pmus_private_key_path: str = ""        # Ed25519 PEM
    pmus_base_url: str = "https://clob.polymarket.us"   # VERIFY

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
            omen_account_size=f("PE_OMEN_ACCOUNT_SIZE", cls.omen_account_size),
            omen_per_trade_fraction=f("PE_OMEN_PER_TRADE_FRACTION", cls.omen_per_trade_fraction),
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
            board_max_tickets=int(f("PE_BOARD_MAX_TICKETS", cls.board_max_tickets)),
            journal_path=e.get("PE_JOURNAL_PATH", cls.journal_path),
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
            pmus_enabled=_truthy(e.get("PE_PMUS_ENABLED"), cls.pmus_enabled),
            pmus_key_id=e.get("PMUS_API_KEY_ID", ""),
            pmus_private_key_path=e.get("PMUS_PRIVATE_KEY_PATH", ""),
            pmus_base_url=e.get("PMUS_BASE_URL", cls.pmus_base_url),
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
