"""PredictionEdge scanner - read-only opportunity printer.

A thin, human-facing view over the same scan the autonomous runner uses. It prints
opportunities and records them to the paper ledger; it never places orders. For
unattended trading use ``python -m predictionedge.run`` instead.

    python -m predictionedge.main            # auto: mock unless live creds present
    python -m predictionedge.main --mock     # force offline canned data
    python -m predictionedge.main --loop --interval 60
    python -m predictionedge.main --summary  # print paper-ledger summary and exit
"""

from __future__ import annotations

import argparse
import time

from .config import Config
from .kalshi import LiveKalshiReadOnlyClient, MockKalshiClient
from .ledger import PaperLedger
from .odds import MockOddsProvider, TheOddsApiProvider
from .scanner import find_opportunities
from .whales import build_whale_provider


def build(cfg: Config, force_mock: bool):
    if cfg.has_live_credentials and not force_mock:
        return (TheOddsApiProvider(cfg.odds_api_key, sports=cfg.odds_sports),
                LiveKalshiReadOnlyClient(cfg.kalshi_base_url, cfg.kalshi_key_id,
                                         cfg.kalshi_private_key_path),
                "LIVE (read-only)")
    return MockOddsProvider(), MockKalshiClient(), "MOCK"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="predictionedge")
    ap.add_argument("--mock", action="store_true", help="force offline canned data")
    ap.add_argument("--loop", action="store_true", help="scan repeatedly")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between loops")
    ap.add_argument("--summary", action="store_true", help="print paper-ledger summary and exit")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    ledger = PaperLedger(cfg.paper_ledger_path)

    if args.summary:
        print(ledger.summary())
        return 0

    odds, kalshi, mode = build(cfg, args.mock)
    whales = build_whale_provider(cfg, mock=(mode == "MOCK"))
    print(f"PredictionEdge v0.1 - mode={mode}  bankroll=${cfg.bankroll:.0f}  "
          f"min_edge={cfg.min_edge*100:.1f}c  devig={cfg.devig_method}  "
          f"whale_weight={cfg.whale_weight}")

    while True:
        print(f"[scan] {time.strftime('%Y-%m-%d %H:%M:%S')}")
        results = find_opportunities(cfg, odds, kalshi, whales)
        if not results:
            print("  no opportunities clear the edge threshold")
        for r in results:
            print("  " + r.opp.describe())
            ledger.record(r.opp, note=r.label)
        if not args.loop:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
