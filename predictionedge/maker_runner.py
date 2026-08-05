"""LIP maker bot entry point — separate loop from the main scanner/copy runner.

Quoting wants a much faster cadence (seconds, to keep LIP uptime scoring and
react to book moves) than the 5-minute scan cycle in run.py, so it gets its
own process:

    python -m predictionedge.maker_runner            # one cycle, then exit
    python -m predictionedge.maker_runner --daemon    # loop forever at --interval
    python -m predictionedge.maker_runner --mock      # MockMakerVenue, no network

Safety: MakerConfig.dry_run defaults True (PE_MAKER_DRY_RUN unset = dry-run).
Flipping it live also requires PE_LIVE_TRADING=1 and PE_ARM (the existing
control.armed() switch) — this reuses the main bot's arm/kill-switch so one
dashboard button and one STOP file govern both engines.
"""

from __future__ import annotations

import argparse
import time

from . import control
from .config import Config
from .kalshi import LiveKalshiTradingClient
from .maker import KalshiMakerVenue, MakerConfig, MakerEngine, MockMakerVenue
from .risk import RiskManager
from .state import StateStore


def build_engine(cfg: Config, maker_cfg: MakerConfig, force_mock: bool) -> MakerEngine:
    if force_mock or not cfg.has_live_credentials:
        return MakerEngine(maker_cfg, MockMakerVenue())
    client = LiveKalshiTradingClient(
        cfg.active_base_url, cfg.kalshi_key_id, cfg.kalshi_private_key_path
    )
    return MakerEngine(maker_cfg, KalshiMakerVenue(client))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="predictionedge.maker_runner")
    ap.add_argument("--daemon", action="store_true", help="loop forever at --interval")
    ap.add_argument("--interval", type=float, default=15.0, help="seconds between cycles")
    ap.add_argument("--mock", action="store_true", help="force MockMakerVenue, no network")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    maker_cfg = MakerConfig.from_env()
    state = StateStore(cfg.state_db_path)
    risk = RiskManager(cfg, state)

    if not maker_cfg.series_allowlist and not args.mock:
        print("PE_MAKER_SERIES is empty — nothing vetted to quote. "
              "Set it (comma-separated series tickers) after manually confirming "
              "LIP eligibility on the program page. Exiting.")
        return 0

    engine = build_engine(cfg, maker_cfg, args.mock)
    live_desc = "DRY-RUN" if maker_cfg.dry_run else "LIVE"
    print(f"PredictionEdge maker bot — mode={live_desc}  "
          f"markets={maker_cfg.series_allowlist or '(mock)'}  "
          f"quote_size={maker_cfg.quote_size}  half_spread={maker_cfg.half_spread_cents}c")

    def run_one() -> None:
        pre = risk.preflight()
        if not pre.allowed:
            print(f"[{time.strftime('%H:%M:%S')}] preflight blocked: {pre.reason}")
            return
        if not control.armed(cfg) and not maker_cfg.dry_run:
            print(f"[{time.strftime('%H:%M:%S')}] not armed — dashboard switch is off")
            return
        actions = engine.cycle()
        acted = [a for a in actions if a.kind != "hold"]
        print(f"[{time.strftime('%H:%M:%S')}] cycle: {len(acted)} action(s)")

    run_one()
    if args.daemon:
        while True:
            time.sleep(args.interval)
            run_one()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
