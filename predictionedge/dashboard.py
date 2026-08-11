"""Sleek local dashboard - see what the bot likes, what it wants to do, and the
smart-money it found.

    python -m predictionedge.dashboard        # then open http://127.0.0.1:8787

Read-only: it scans and previews; it never places an order. Uses your Kalshi key
for an account balance check (read-only) and public Polymarket data for whales.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import control
from .config import Config
from .copytrade import find_copy_signals
from .crypto import CoinbaseData, find_crypto_edges
from .flow import find_spikes
from .gamma import end_dates
from .ledger import PaperLedger
from .risk import RiskManager
from .scanner import find_opportunities, resolve_links
from .state import StateStore
from .whales import build_whale_provider

_TEMPLATE = Path(__file__).with_name("dashboard.html")
_HISTORY = Path(__file__).with_name("history.html")
_BOARD = Path(__file__).with_name("board.html")


def _consistency_section(cfg: Config) -> dict:
    """Live logical-consistency violations, as an ADVISORY section - never tickets.

    Deliberately not merged into ``tickets``: a mechanical violation has no whale, so
    drift_c/n_wallets/whale_price would all be fiction, and EDGE_PLAN step 4 gates any
    new source out of the acted-on list until it has printed sane live numbers under
    review. This surfaces the numbers so that review can happen.
    """
    if not cfg.consistency_enabled:
        return {"ok": True, "enabled": False, "violations": [], "notes": [],
                "rejected": {}, "events_scanned": 0}
    try:
        from .consistency import scan
        rep = scan(cfg)
    except Exception as exc:  # noqa: BLE001
        # An outage must read as "we do not know", never as "the book is clean".
        return {"ok": False, "enabled": True, "violations": [], "rejected": {},
                "events_scanned": 0,
                "notes": [f"consistency scan failed, result unknown: {exc}"]}
    return {
        "ok": True,
        "enabled": True,
        "events_scanned": rep.events_scanned,
        "rejected": rep.rejected,
        "notes": rep.notes,
        "violations": [{
            "family": v.family, "event_slug": v.event_slug,
            "constraint": v.constraint, "edge_c": round(v.edge_c, 2),
            "actions": list(v.actions), "warnings": list(v.warnings),
            "url": v.legs[0].url if v.legs else "",
            "legs": [{"question": lg.question, "name": lg.name, "bid": lg.bid,
                      "ask": lg.ask, "liquidity": lg.liquidity,
                      "end_iso": lg.end_iso, "url": lg.url} for lg in v.legs],
        } for v in rep.violations],
    }


def _macro_section(cfg: Config) -> dict:
    """CPI brackets priced against the Cleveland Fed nowcast. Advisory, same as above.

    The Fed-futures leg is deliberately absent: no free terms-compliant source for the
    strip was found, and macrofv fails closed rather than substitute a scrape.
    """
    if not cfg.macro_enabled:
        return {"ok": True, "enabled": False, "cpi": [], "notes": []}
    try:
        from .macrofv import run_cpi
        comparisons = run_cpi(cfg)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "enabled": True, "cpi": [],
                "notes": [f"macro fair-value failed, result unknown: {exc}"]}
    return {
        "ok": True,
        "enabled": True,
        "notes": [],
        "cpi": [{
            "slug": c.match.slug, "title": c.match.title,
            "series": c.match.series, "basis": c.match.basis,
            "ref_month": c.match.ref_month,
            "url": f"https://polymarket.com/event/{c.match.slug}",
            "nowcast_pp": c.nowcast.value_pp, "nowcast_as_of": c.nowcast.as_of,
            "sigma": c.sigma,
            # Carried verbatim so the page cannot quietly drop the one caveat that
            # explains away a whole column of apparent edges.
            "model_sigma_note": c.model_sigma_note,
            "rows": [{"label": r.bracket.label, "market_prob": r.bracket.market_prob,
                      "model_prob": round(r.model_prob, 4),
                      "gap_c": round(r.gap_c, 2), "suspect": r.suspect}
                     for r in c.rows],
        } for c in comparisons],
    }


def build_board_payload(cfg: Config, force_mock: bool = False) -> dict:
    """The manual trade board: ranked suggestions + what we filtered out + the journal."""
    from .journal import Journal
    from .omen import OmenAccount
    from .signals import build_board
    from .whales import build_whale_provider

    whales = build_whale_provider(cfg, mock=force_mock, enrich=False)
    client = getattr(whales, "client", None)
    scorer = getattr(whales, "scorer", None)
    account = OmenAccount(size=cfg.omen_account_size,
                          per_trade_fraction=cfg.omen_per_trade_fraction)

    if client is None or scorer is None:
        report = None
    else:
        report = build_board(cfg, client, scorer, account)

    journal = Journal(cfg.journal_path)

    def _row(t):
        return {
            "market_id": t.market_id, "title": t.title, "side_label": t.side_label,
            "outcome": t.outcome, "entry_price": t.entry_price,
            "whale_price": t.whale_price, "drift_c": t.drift_c,
            "contracts": t.contracts, "cost": t.cost,
            "max_payout": t.sizing.max_payout, "conviction": t.conviction,
            "hours_to_resolve": t.hours_to_resolve, "n_wallets": t.n_wallets,
            "whale_usd": t.whale_usd, "minutes_ago": t.minutes_ago,
            "liquidity": t.liquidity, "url": t.url,
            "end_iso": t.end_iso, "event_iso": t.event_iso,
            "signal_ts": t.signal_ts,
            "why": t.why, "warnings": t.warnings,
        }

    tickets = [_row(t) for t in (report.tickets if report else [])]
    # Cleared every hard filter, sits below the buy bar. Published so the paper trial can
    # observe the rejected region - the only way the bar itself ever becomes checkable.
    # Kept out of `tickets` because these are measurements, not recommendations.
    probe = [_row(t) for t in (report.probe if report else [])]

    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "generated_at": time.time(),
        "account_size": cfg.omen_account_size,
        "tickets": tickets,
        "probe": probe,
        "considered": report.considered if report else 0,
        "rejected": report.rejected if report else {},
        "notes": (report.notes if report else ["whale data source unavailable"]),
        "journal": journal.summary(),
        "journal_state": journal.latest(),
        # Advisory-only sources (EDGE_PLAN E1/E3). Skipped under force_mock: both do a
        # live crawl, and a mock run must stay offline.
        "consistency": ({"ok": True, "enabled": False, "violations": [], "notes":
                         ["skipped in mock mode"], "rejected": {}, "events_scanned": 0}
                        if force_mock else _consistency_section(cfg)),
        "macro": ({"ok": True, "enabled": False, "cpi": [],
                   "notes": ["skipped in mock mode"]}
                  if force_mock else _macro_section(cfg)),
    }


def _pick_and_url(market, side: str) -> tuple[str, str]:
    """Human 'who is it backing' label + the Kalshi web link.

    Sports markets carry the team in yes_sub_title, so 'YES on the SF market' reads as
    'San Francisco to win'. Politics/binary markets have no team -> plain YES/NO (the
    market title already states the question).
    """
    url = market.web_url() if market is not None else ""
    team = (getattr(market, "yes_sub_title", "") or "") if market is not None else ""
    pick = f"{team} to {'win' if side == 'yes' else 'lose'}" if team else side.upper()
    return pick, url


def build_snapshot(cfg: Config, force_mock: bool = False) -> dict:
    from .kalshi import (
        LiveKalshiReadOnlyClient,
        LiveKalshiTradingClient,
        MockKalshiClient,
    )
    from .odds import MockOddsProvider, TheOddsApiProvider

    has_kalshi = bool(cfg.kalshi_key_id and cfg.kalshi_private_key_path) and not force_mock
    has_odds = bool(cfg.odds_api_key) and not force_mock

    kalshi = (LiveKalshiReadOnlyClient(cfg.kalshi_base_url, cfg.kalshi_key_id,
                                       cfg.kalshi_private_key_path)
              if has_kalshi else MockKalshiClient())
    whales = build_whale_provider(cfg, mock=force_mock, enrich=False)  # fast preview
    state = StateStore(cfg.state_db_path)
    risk = RiskManager(cfg, state)

    # Sports de-vig edges (only when the Odds API key is funded; real, never demo).
    likes = []
    if has_odds:
        odds = TheOddsApiProvider(cfg.odds_api_key, sports=cfg.odds_sports)
        for r in find_opportunities(cfg, odds, kalshi, whales):
            vet = risk.vet(r.opp)
            pick, url = _pick_and_url(r.market, r.opp.side)
            likes.append({
                "ticker": r.opp.ticker, "label": r.label, "side": r.opp.side.upper(),
                "pick": pick, "url": url,
                "fair": round(r.fair_yes, 3), "price": r.opp.price,
                "edge_c": round(r.opp.edge_per_contract * 100, 2),
                "contracts": r.opp.contracts, "ev": round(r.opp.expected_value, 2),
                "would_act": vet.allowed, "reason": vet.reason,
            })

    # Crypto statistical edges (lognormal spot+vol model) - shown alongside de-vig.
    if not force_mock and cfg.crypto_enabled:
        for c in find_crypto_edges(cfg, kalshi, CoinbaseData()):
            vet = risk.vet(c.opp)
            likes.append({
                "ticker": c.opp.ticker, "label": f"{c.market.title} (model)",
                "side": c.opp.side.upper(),
                "pick": f"{c.opp.side.upper()} · {c.market.yes_sub_title}",
                "url": c.market.web_url(),
                "fair": round(c.model_prob, 3), "price": c.opp.price,
                "edge_c": round(c.opp.edge_per_contract * 100, 2),
                "contracts": c.opp.contracts, "ev": round(c.opp.expected_value, 2),
                "would_act": vet.allowed, "reason": vet.reason,
            })
        likes.sort(key=lambda o: o["ev"], reverse=True)

    from .whale_edge import find_whale_edges

    # Smart-money leaderboard - the politics/econ crowd, where insider edge lives.
    whale_top, whale_cat = [], "POLITICS"
    client = getattr(whales, "client", None)
    if client is not None:
        rows = []
        try:
            rows = client.leaderboard("POLITICS")
        except Exception:  # noqa: BLE001
            rows = []
        if not rows:
            whale_cat = "OVERALL"
            try:
                rows = client.leaderboard("OVERALL")
            except Exception:  # noqa: BLE001
                rows = []
        for w in rows[:12]:
            whale_top.append({"address": w.address, "pnl": round(w.realized_pnl),
                              "volume": round(w.volume)})

    # Whale-driven edges: politics/econ markets in the whale map where smart money
    # diverges from the Kalshi price (the signal is the fair value here).
    whale_edges = []
    for w in find_whale_edges(cfg, kalshi, whales):
        vet = risk.vet(w.opp)
        pick, url = _pick_and_url(w.market, w.opp.side)
        whale_edges.append({
            "ticker": w.opp.ticker, "label": w.label, "side": w.opp.side.upper(),
            "pick": pick, "url": url,
            "fair": round(w.signal.prob, 3), "price": w.opp.price,
            "edge_c": round(w.opp.edge_per_contract * 100, 2),
            "confidence": round(w.signal.confidence, 2),
            "contracts": w.opp.contracts, "ev": round(w.opp.expected_value, 2),
            "would_act": vet.allowed, "reason": vet.reason,
        })

    # Copy-trade signals: profitable wallets buying now (the Polymarket-native edge).
    copy_signals = []
    scorer = getattr(whales, "scorer", None)
    if client is not None and scorer is not None and cfg.copytrade_enabled:
        try:
            for s in find_copy_signals(client, scorer, categories=cfg.copytrade_categories,
                                       min_usd=cfg.copytrade_min_usd,
                                       min_wallets=cfg.copytrade_min_wallets,
                                       max_price=cfg.copytrade_max_price)[:14]:
                copy_signals.append({
                    "title": s.title, "outcome": s.outcome, "n_wallets": s.n_wallets,
                    "usd": round(s.total_usd), "avg_price": round(s.avg_price, 2),
                    "minutes_ago": round(s.minutes_ago),
                    "url": ("https://polymarket.com/event/" + s.event_slug) if s.event_slug else "",
                })
        except Exception:  # noqa: BLE001
            pass

    # Whale trade spikes: sudden large bets on event markets (the insider-flow feed).
    spikes = []
    if client is not None:
        try:
            raw = find_spikes(client, min_usd=25000, limit=500)[:18]
        except Exception:  # noqa: BLE001
            raw = []
        ends = end_dates([s.trade.condition_id for s in raw]) if raw else {}
        now = datetime.now(timezone.utc)
        for s in raw:
            t = s.trade
            closes_in = None
            ed = ends.get(t.condition_id)
            if ed:
                try:
                    closes_in = round(
                        (datetime.fromisoformat(ed.replace("Z", "+00:00")) - now)
                        .total_seconds() / 86400.0, 1)
                except Exception:  # noqa: BLE001
                    pass
            spikes.append({
                "title": t.title, "outcome": t.outcome, "side": t.side,
                "size": round(t.size), "price": round(t.price, 3),
                "minutes_ago": round(s.minutes_ago), "category": s.category,
                "trader": t.name or (t.wallet[:6] + "…" + t.wallet[-4:] if t.wallet else ""),
                "url": ("https://polymarket.com/event/" + t.event_slug) if t.event_slug else "",
                "closes_in_days": closes_in,
            })

    # Account (read-only balance check against prod, where a real key authenticates).
    account = {"connected": False, "balance": None, "detail": "no Kalshi credentials"}
    if has_kalshi:
        try:
            tc = LiveKalshiTradingClient(cfg.kalshi_base_url, cfg.kalshi_key_id,
                                         cfg.kalshi_private_key_path)
            account = {"connected": True, "balance": tc.balance_dollars(),
                       "detail": "prod (read-only)"}
        except Exception as exc:  # noqa: BLE001
            account = {"connected": False, "balance": None,
                       "detail": f"auth check failed: {exc}"}

    hb = {}
    p = Path(cfg.heartbeat_path)
    if p.exists():
        try:
            hb = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            hb = {}

    snap = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": ("LIVE" if cfg.live_trading else "DRY-RUN") + (" / DEMO" if cfg.use_demo else ""),
        "live_trading": cfg.live_trading,
        "live_capable": bool(cfg.live_trading and has_kalshi),
        "armed": control.armed(cfg),
        "sources": {"kalshi": has_kalshi, "odds": has_odds,
                    "polymarket": cfg.whale_source == "polymarket"},
        "account": account,
        "caps": {"bankroll": cfg.bankroll, "max_total_exposure": cfg.max_total_exposure,
                 "max_open_positions": cfg.max_open_positions,
                 "max_daily_loss": cfg.max_daily_loss, "min_edge_c": round(cfg.min_edge * 100, 1)},
        "likes": likes, "odds_offline": not has_odds,
        "whale_edges": whale_edges, "whale_top": whale_top, "whale_category": whale_cat,
        "spikes": spikes, "copy_signals": copy_signals,
        "exposure": round(state.open_exposure(), 2),
        "open_positions": state.open_position_count(),
        "today_pnl": round(state.today_realized_pnl(), 2),
        "ledger": PaperLedger(cfg.paper_ledger_path).summary(),
        "heartbeat": hb,
    }
    state.close()
    return snap


def run_engine(cfg: Config, wake) -> None:
    """Background trading loop: runs cycles forever; each cycle is dry until the ARM
    button is on (control.armed), then it places real orders within the caps. Lives in
    the dashboard process so arming the button immediately drives the bot."""
    from .runner import TradingRunner
    runner = TradingRunner(cfg)  # constructed in this thread (sqlite is thread-bound)
    while True:
        try:
            runner.cycle()
        except Exception:  # noqa: BLE001 - the runner logs; never let the loop die
            pass
        wake.wait(cfg.poll_interval)  # wake early when the arm switch is toggled
        wake.clear()


def create_app(cfg: Config, force_mock: bool = False, ttl: float = 60.0, wake=None):
    from flask import Flask, Response, jsonify, request

    app = Flask(__name__)
    cache: dict = {"ts": 0.0, "data": None}
    board_cache: dict = {"ts": 0.0, "data": None}

    def snapshot():
        now = time.time()
        if cache["data"] is None or now - cache["ts"] > ttl:
            cache["data"] = build_snapshot(cfg, force_mock)
            cache["ts"] = now
        return cache["data"]

    @app.get("/")
    def index() -> Response:
        return Response(_TEMPLATE.read_text(encoding="utf-8"), mimetype="text/html")

    @app.get("/api/snapshot")
    def api_snapshot():
        return jsonify(snapshot())

    @app.get("/history")
    def history() -> Response:
        return Response(_HISTORY.read_text(encoding="utf-8"), mimetype="text/html")

    @app.get("/api/orders")
    def api_orders():
        st = StateStore(cfg.state_db_path)
        try:
            orders = [{k: r[k] for k in r.keys()} for r in st.recent_orders(300)]
        finally:
            st.close()
        return jsonify({"orders": orders})

    @app.get("/board")
    def board() -> Response:
        return Response(_BOARD.read_text(encoding="utf-8"), mimetype="text/html")

    @app.get("/api/board")
    def api_board():
        now = time.time()
        if board_cache["data"] is None or now - board_cache["ts"] > ttl:
            board_cache["data"] = build_board_payload(cfg, force_mock)
            board_cache["ts"] = now
        return jsonify(board_cache["data"])

    @app.post("/api/journal")
    def api_journal():
        from .journal import Journal, JournalEntry

        body = request.get_json(silent=True) or {}
        tid, status = body.get("id"), body.get("status")
        if not tid or status not in ("taken", "skipped", "won", "lost"):
            return jsonify({"error": "id and a valid status are required"}), 400

        # Re-use the ticket that is on screen so the log keeps price/size as advised.
        payload = board_cache["data"] or {}
        match = next((t for t in payload.get("tickets", [])
                      if f"{t['market_id']}:{t['outcome']}" == tid), None)
        prior = Journal(cfg.journal_path).latest().get(tid, {})
        src = match or prior

        journal = Journal(cfg.journal_path)
        journal.record(JournalEntry(
            id=tid, ts=time.time(),
            title=src.get("title", ""), side_label=src.get("side_label", ""),
            entry_price=float(src.get("entry_price") or 0.0),
            contracts=int(src.get("contracts") or 0),
            cost=float(src.get("cost") or 0.0),
            conviction=float(src.get("conviction") or 0.0),
            status=status, note=str(body.get("note") or ""),
        ))
        # Refresh only the journal half of the cached payload: the decision must show
        # up immediately, but re-scanning every market on a button click would not.
        if board_cache["data"] is not None:
            board_cache["data"]["journal"] = journal.summary()
            board_cache["data"]["journal_state"] = journal.latest()
        return jsonify({"ok": True, "id": tid, "status": status})

    @app.post("/api/trading")
    def api_trading():
        if cfg.research_only:
            return jsonify({"error": "research_only mode: this build does not trade"}), 403
        body = request.get_json(silent=True) or {}
        control.set_armed(cfg, bool(body.get("armed")))
        cache["data"] = None  # force the next snapshot to reflect the new state
        if wake is not None:
            wake.set()         # run a trading cycle now, not at the next interval
        return jsonify({"armed": control.armed(cfg)})

    return app


def main(argv: list[str] | None = None) -> int:
    import threading

    ap = argparse.ArgumentParser(prog="predictionedge.dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--mock", action="store_true", help="force offline canned data")
    ap.add_argument("--no-engine", action="store_true", help="UI only; no trading loop")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    wake = threading.Event()
    if cfg.research_only:
        print("research-only mode: advisory board, no trading engine, no orders.")
    elif not args.mock and not args.no_engine:
        threading.Thread(target=run_engine, args=(cfg, wake), daemon=True).start()
        print(f"trading engine running every {cfg.poll_interval:.0f}s "
              f"(dry until you ARM it on the dashboard)")

    app = create_app(cfg, force_mock=args.mock, wake=wake)
    print(f"PredictionEdge trade board -> http://{args.host}:{args.port}/board")
    print(f"           full dashboard -> http://{args.host}:{args.port}/  (Ctrl+C to stop)")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
