"""Write the trade board to a static JSON file for GitHub Pages.

Pages serves static files only, so the Flask app cannot run there. Instead a
scheduled GitHub Action runs *this* - the same tested `signals.build_board` the local
dashboard uses - and commits the result, which the static page renders. The filter
logic therefore has one implementation, not a Python one and a drifting JavaScript
copy of it.

Everything this reads is public, unauthenticated Polymarket data, so the workflow
needs no secrets. Nothing about your capital, positions or journal is included:
the journal lives in your browser's localStorage and is never published.

    python -m predictionedge.publish --out docs/board.json
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from . import sizing
from .config import Config


# The NBM station-guidance cache lives next to the trial for the same reason the
# trial does: the workflow checks out fresh every 15 minutes, and committing the file
# is what lets a run remember which bulletin cycles it has already downloaded.
NBM_CACHE = "docs/nbm.json"


def weather_tickets(cfg: Config, nbm_cache_path: str = NBM_CACHE) -> list[dict]:
    """The NWS/Kalshi sleeve, in its OWN array - never merged into `tickets`.

    Two sleeves, two hypotheses. The board's tickets claim "whales pick winners"; these
    claim "the NWS locates tomorrow's high better than the Kalshi ladder does". Pooling
    them into one win rate would answer neither question, which is the same mistake
    `papertrial` avoids by holding conviction flat. They are recorded separately and
    scored separately.

    Never raises. A weather outage must not take the whale board down with it.
    """
    if not cfg.weather_enabled:
        return []
    cache_file, cache = Path(nbm_cache_path), {}
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cache = {}                       # absent or corrupt: rebuilt from the bulletins
    try:
        from .weather import find_weather_edges
        tickets = find_weather_edges(min_edge=cfg.weather_min_edge,
                                     max_days=cfg.weather_max_days,
                                     nbm_cache=cache)
    except Exception as exc:  # noqa: BLE001
        print(f"weather sleeve unavailable: {exc}")
        return []
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"nbm cache not saved: {exc}")   # next run just re-downloads one cycle
    return tickets


def build_payload(cfg: Config) -> dict:
    from .dashboard import build_board_payload

    payload = build_board_payload(cfg)
    payload["weather"] = weather_tickets(cfg)
    # The journal is personal and browser-local; never publish it.
    payload.pop("journal", None)
    payload.pop("journal_state", None)
    # The page re-sizes every ticket against your *current* capital, so it needs the
    # same rules the Python sizer uses. Publish them instead of hardcoding a second
    # copy in JavaScript that can silently drift out of step with sizing.py.
    payload["sizing"] = {
        "min_contract_price": sizing.MIN_CONTRACT_PRICE,
        "contracts_per_market_per_1k": sizing.CONTRACTS_PER_MARKET_PER_1K,
        "contracts_per_event_per_1k": sizing.CONTRACTS_PER_EVENT_PER_1K,
        "per_trade_fraction": cfg.board_per_trade_fraction,
        "daily_loss_fraction": 0.05,
    }
    # Pre-rename key; kept for one transition so a cached page's JS keeps sizing.
    payload["omen"] = payload["sizing"]
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
                   "generated_at": time.time(), "tickets": [], "weather": [],
                   "considered": 0,
                   "rejected": {}, "notes": [f"board generation failed: {exc}"],
                   "account_size": cfg.board_account_size,
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
          f"from {payload['considered']} signal(s), "
          f"{len(payload.get('weather') or [])} weather")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
