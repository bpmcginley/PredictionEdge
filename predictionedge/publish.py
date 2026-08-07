"""Write the trade board to a static JSON file for GitHub Pages.

Pages serves static files only, so the Flask app cannot run there. Instead a
scheduled GitHub Action runs *this* - the same tested `signals.build_board` the local
dashboard uses - and commits the result, which the static page renders. The filter
logic therefore has one implementation, not a Python one and a drifting JavaScript
copy of it.

Everything this reads is public, unauthenticated Polymarket data, so the workflow
needs no secrets. Nothing about your Omen account, positions or journal is included:
the journal lives in your browser's localStorage and is never published.

    python -m predictionedge.publish --out docs/board.json
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from . import omen
from .config import Config


def build_payload(cfg: Config) -> dict:
    from .dashboard import build_board_payload

    payload = build_board_payload(cfg)
    # The journal is personal and browser-local; never publish it.
    payload.pop("journal", None)
    payload.pop("journal_state", None)
    # The page re-sizes every ticket against your *current* capital, so it needs the
    # same rules the Python sizer uses. Publish them instead of hardcoding a second
    # copy in JavaScript that can silently drift out of step with omen.py.
    payload["omen"] = {
        "min_contract_price": omen.MIN_CONTRACT_PRICE,
        "contracts_per_market_per_1k": omen.CONTRACTS_PER_MARKET_PER_1K,
        "contracts_per_event_per_1k": omen.CONTRACTS_PER_EVENT_PER_1K,
        "per_trade_fraction": cfg.omen_per_trade_fraction,
        "daily_loss_fraction": 0.05,
    }
    payload["filters"] = {
        "min_hours": cfg.board_min_hours,
        "max_days": cfg.board_max_days,
        "max_drift_c": cfg.board_max_drift_c,
        "min_conviction": cfg.board_min_conviction,
        "min_whale_usd": cfg.copytrade_min_usd,
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="predictionedge.publish")
    ap.add_argument("--out", default="docs/board.json")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    try:
        payload = build_payload(cfg)
    except Exception as exc:  # noqa: BLE001
        # A data outage must publish an honest empty board, not break the site.
        payload = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "generated_at": time.time(), "tickets": [], "considered": 0,
                   "rejected": {}, "notes": [f"board generation failed: {exc}"],
                   "account_size": cfg.omen_account_size,
                   # ok=False, not an empty list: a failed run must not render as a
                   # clean book on the page.
                   "consistency": {"ok": False, "enabled": True, "violations": [],
                                   "rejected": {}, "events_scanned": 0,
                                   "notes": ["board generation failed"]},
                   "macro": {"ok": False, "enabled": True, "cpi": [],
                             "notes": ["board generation failed"]}}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out} — {len(payload['tickets'])} ticket(s) "
          f"from {payload['considered']} signal(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
