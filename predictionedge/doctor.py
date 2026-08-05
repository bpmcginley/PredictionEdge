"""Preflight readiness check - run this before scheduling or going live.

    python -m predictionedge.doctor            # full check (incl. network)
    python -m predictionedge.doctor --no-network

Verifies config + caps, credentials, the private key loads, kill-switch state,
trading posture, and (optionally) that Kalshi + The Odds API are reachable. Exits 0
when ready to collect live read-only data.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import Config

Check = tuple[str, bool, str]


def _private_key_ok(cfg: Config) -> Check:
    p = Path(cfg.kalshi_private_key_path)
    if not p.exists():
        return ("kalshi private key", False, f"file not found: {p}")
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        load_pem_private_key(p.read_bytes(), password=None)
        return ("kalshi private key", True, "RSA key loads")
    except Exception as exc:  # noqa: BLE001
        return ("kalshi private key", False, f"unreadable: {exc}")


def _kalshi_reachable(cfg: Config) -> Check:
    try:
        import requests
        r = requests.get(cfg.active_base_url + "/markets", params={"limit": 1}, timeout=10)
        return ("kalshi reachable", r.status_code == 200,
                f"GET /markets -> {r.status_code} @ {cfg.active_base_url}")
    except Exception as exc:  # noqa: BLE001
        return ("kalshi reachable", False, f"unreachable: {exc}")


def _odds_reachable(cfg: Config) -> Check:
    if not cfg.odds_api_key:
        return ("odds reachable", False, "no ODDS_API_KEY")
    try:
        import requests
        r = requests.get("https://api.the-odds-api.com/v4/sports",
                         params={"apiKey": cfg.odds_api_key}, timeout=10)
        left = r.headers.get("x-requests-remaining", "?")
        return ("odds reachable", r.status_code == 200,
                f"GET /v4/sports -> {r.status_code}, credits left={left}")
    except Exception as exc:  # noqa: BLE001
        return ("odds reachable", False, f"unreachable: {exc}")


def run_checks(cfg: Config, *, network: bool = True) -> list[Check]:
    checks: list[Check] = [
        ("config", True, f"bankroll=${cfg.bankroll:.0f}  min_edge={cfg.min_edge}  "
         f"caps: ${cfg.max_total_exposure:.0f} total / {cfg.max_open_positions} pos / "
         f"${cfg.max_daily_loss:.0f} daily-loss"),
    ]
    has_creds = bool(cfg.kalshi_key_id and cfg.kalshi_private_key_path)
    checks.append(("kalshi creds", has_creds,
                   "present" if has_creds else "missing KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH"))
    if cfg.kalshi_private_key_path:
        checks.append(_private_key_ok(cfg))
    checks.append(("odds api key", bool(cfg.odds_api_key),
                   "present" if cfg.odds_api_key else "missing ODDS_API_KEY"))

    ks_clear = not Path(cfg.killswitch_path).exists()
    checks.append(("kill switch", ks_clear,
                   "clear" if ks_clear else f"ENGAGED ({cfg.killswitch_path}) - trading halted"))

    mode = (f"LIVE TRADING ({'DEMO' if cfg.use_demo else 'PROD'})"
            if cfg.live_trading else "DRY-RUN (no real orders)")
    checks.append(("trading mode", True, mode))
    checks.append(("whale source", True,
                   f"{cfg.whale_source} (weight {cfg.whale_weight})"
                   if cfg.whale_source != "none" else "none"))
    checks.append(("discovery", True,
                   "on: " + ",".join(cfg.sports_series) if cfg.dynamic_discovery
                   else "off (static links)"))

    if network:
        checks.append(_kalshi_reachable(cfg))
        checks.append(_odds_reachable(cfg))
    return checks


def ready_for_live_data(checks: list[Check]) -> bool:
    """Ready to collect real read-only data: creds + reachable + kill-switch clear."""
    need = {"kalshi creds", "kalshi private key", "odds api key", "kill switch",
            "kalshi reachable", "odds reachable"}
    status = {name: ok for name, ok, _ in checks}
    return all(status.get(n, False) for n in need if n in status)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="predictionedge.doctor")
    ap.add_argument("--no-network", action="store_true", help="skip connectivity checks")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    checks = run_checks(cfg, network=not args.no_network)
    width = max(len(n) for n, _, _ in checks)
    for name, ok, msg in checks:
        print(f"[{'OK' if ok else 'XX'}] {name.ljust(width)}  {msg}")

    ready = ready_for_live_data(checks)
    print()
    if ready:
        print("READY: schedule dry-run on live data with scripts\\register_task.ps1")
    elif not (cfg.kalshi_key_id and cfg.odds_api_key):
        print("Runs in MOCK mode only (no real data) until you add KALSHI_API_KEY_ID, "
              "KALSHI_PRIVATE_KEY_PATH, and ODDS_API_KEY to .env.")
    else:
        print("NOT READY: resolve the [XX] items above before scheduling.")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
