"""Autonomous entry point - what the Windows scheduler invokes.

    python -m predictionedge.run            # one cycle, then exit (scheduler-friendly)
    python -m predictionedge.run --daemon   # loop forever at poll_interval
    python -m predictionedge.run --status    # print heartbeat + ledger and exit
    python -m predictionedge.run --mock      # force offline canned data

Trading is dry-run unless PE_LIVE_TRADING=1, and even then defaults to Kalshi's
demo environment. No Claude involvement at runtime.
"""

from __future__ import annotations

import argparse
import json

from .config import Config
from .runner import TradingRunner


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="predictionedge.run")
    ap.add_argument("--daemon", action="store_true", help="loop forever at poll_interval")
    ap.add_argument("--status", action="store_true", help="print status and exit")
    ap.add_argument("--mock", action="store_true", help="force offline canned data")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    runner = TradingRunner(cfg, force_mock=args.mock)

    if args.status:
        print(json.dumps(runner.status(), indent=2))
        return 0
    if args.daemon:
        runner.run_forever()
        return 0
    runner.run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
