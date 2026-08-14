"""The autonomous runner - one ``cycle()`` is a full pass the scheduler can fire.

Cycle order matters: we review/​hedge existing orders FIRST (risk mitigation before
new risk), then scan for new edges. Every cycle writes a heartbeat file so an
external watcher (or you) can see it's alive without opening anything.

Safety posture: dry-run unless ``live_trading`` is explicitly set; live trading
defaults to Kalshi's demo environment; production is blocked until a clean demo
run has been recorded.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import uuid

from . import control
from .config import Config
from .copytrade import copy_order_params, find_copy_signals
from .crypto import CoinbaseData, find_crypto_edges
from .executor import DryRunExecutor, LiveExecutor
from .polymarket_us import BUY_YES, PolymarketUSClient
from .pmus_match import PMUSMatcher
from .kalshi import (
    LiveKalshiReadOnlyClient,
    LiveKalshiTradingClient,
    MockKalshiClient,
)
from .ledger import PaperLedger
from .logging_setup import get_logger
from .odds import MockOddsProvider, TheOddsApiProvider
from .review import review_open_orders
from .risk import RiskManager
from .settle import settle_positions
from .scanner import find_opportunities
from .state import StateStore
from .whale_edge import find_whale_edges
from .whales import build_whale_provider


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradingRunner:
    def __init__(self, cfg: Config, force_mock: bool = False):
        self.cfg = cfg
        self.log = get_logger("predictionedge", cfg.log_path)
        self.state = StateStore(cfg.state_db_path)
        self.ledger = PaperLedger(cfg.paper_ledger_path)
        self.mock = force_mock or not cfg.has_live_credentials
        self.whales = build_whale_provider(cfg, self.mock)
        self.risk = RiskManager(cfg, self.state)

        self.live_capable = cfg.live_trading and not self.mock
        self.crypto_data = None if self.mock else CoinbaseData()
        self.pmus = None
        if not self.mock and cfg.pmus_enabled and cfg.pmus_key_id and cfg.pmus_private_key_path:
            self.pmus = PolymarketUSClient(key_id=cfg.pmus_key_id,
                                           private_key_path=cfg.pmus_private_key_path)
        self.dry_executor = DryRunExecutor(cfg, self.state, self.ledger, self.log)
        self.live_executor = None
        self.odds, self.kalshi, self.base_mode = self._build()
        self.mode = self.base_mode
        self.log.info("runner ready: %s | live_capable=%s", self.base_mode, self.live_capable)

    def _build(self):
        """Returns (odds, kalshi_client, base_mode); sets self.live_executor if capable.

        The per-cycle choice of dry vs live executor is made later from the arm switch,
        so the capability is built once here and gated dynamically in cycle().
        """
        cfg = self.cfg
        if self.mock:
            return MockOddsProvider(), MockKalshiClient(), "MOCK / DRY-RUN"

        odds = TheOddsApiProvider(cfg.odds_api_key, sports=cfg.odds_sports)

        if self.live_capable:
            prod = not cfg.use_demo
            gate_unmet = (
                prod and cfg.require_demo_first and self.state.get_meta("demo_run_ok") != "1"
            )
            if gate_unmet:
                self.log.warning(
                    "LIVE blocked: require_demo_first set, no clean demo run recorded. "
                    "Running dry. Set PE_REQUIRE_DEMO_FIRST=0 or do a demo run first."
                )
                self.live_capable = False
            else:
                client = LiveKalshiTradingClient(
                    cfg.active_base_url, cfg.kalshi_key_id, cfg.kalshi_private_key_path
                )
                self.live_executor = LiveExecutor(cfg, self.state, self.ledger, self.log, client)
                return odds, client, f"LIVE-CAPABLE ({'DEMO' if cfg.use_demo else 'PROD'})"

        read = LiveKalshiReadOnlyClient(
            cfg.kalshi_base_url, cfg.kalshi_key_id, cfg.kalshi_private_key_path
        )
        return odds, read, "LIVE-DATA / DRY-RUN"

    def _select_executor(self):
        """Live executor only when capable AND armed AND kill-switch clear; else dry."""
        live = (self.live_executor is not None
                and control.armed(self.cfg)
                and not self.risk.killswitch_engaged())
        executor = self.live_executor if live else self.dry_executor
        env = "DEMO" if self.cfg.use_demo else "PROD"
        self.mode = f"LIVE TRADING ({env})" if live else (
            "DRY-RUN (armed off)" if self.live_executor is not None else self.base_mode)
        return executor

    def _run_copy_trades(self) -> int:
        """Mirror profitable Polymarket wallets onto Polymarket US. Live only when the
        arm switch is on AND pmus_enabled AND kill-switch clear; otherwise paper."""
        client = getattr(self.whales, "client", None)
        scorer = getattr(self.whales, "scorer", None)
        if self.pmus is None or client is None or scorer is None or not self.cfg.copytrade_enabled:
            return 0
        try:
            signals = find_copy_signals(
                client, scorer, categories=self.cfg.copytrade_categories,
                min_usd=self.cfg.copytrade_min_usd, min_wallets=self.cfg.copytrade_min_wallets,
                max_price=self.cfg.copytrade_max_price)
        except Exception:  # noqa: BLE001
            self.log.exception("copy-signal scan failed")
            return 0

        live = (self.cfg.pmus_enabled and control.armed(self.cfg)
                and not self.risk.killswitch_engaged())
        dedup = ("placed", "open", "partial", "filled") if live else ("paper",)
        try:
            matcher = PMUSMatcher(self.pmus.active_slugs())
        except Exception:  # noqa: BLE001
            return 0
        placed = 0
        for s in signals:
            if placed >= self.cfg.max_orders_per_cycle or not s.slug:
                continue
            pmus_slug = matcher.match(s.slug)   # intl market -> PM-US market
            if pmus_slug is None:
                continue                         # not listed on PM-US
            ticker = "PMUS:" + pmus_slug
            if self.state.has_active_market(ticker, statuses=dedup):
                continue
            if self.state.open_exposure() + self.cfg.copytrade_size_usd > self.cfg.max_total_exposure:
                break
            params = copy_order_params(s, self.pmus.market(pmus_slug), min_price=self.cfg.min_price,
                                       max_price=self.cfg.copytrade_max_price,
                                       size_usd=self.cfg.copytrade_size_usd)
            if params is None:
                continue
            intent, price, qty = params
            side = "yes" if intent == BUY_YES else "no"
            coid = "pmus-" + uuid.uuid4().hex[:16]
            rec = dict(client_order_id=coid, ticker=ticker, side=side, price=price,
                       contracts=qty, stake=round(price * qty, 4),
                       entry_event_prob=s.avg_price, kind="copy", note="copy " + s.title[:40])
            if live:
                try:
                    resp = self.pmus.create_order(market_slug=pmus_slug, intent=intent,
                                                  price=price, quantity=qty)
                    self.state.record_order(status="open", kalshi_order_id=resp.get("id"), **rec)
                    self.log.info("PM-US COPY %s %s x%d @ %.2f", s.slug, side, qty, price)
                    placed += 1
                except Exception as exc:  # noqa: BLE001
                    self.log.error("PM-US copy FAILED %s: %s", s.slug, exc)
            else:
                self.state.record_order(status="paper", **rec)
                self.log.info("DRY copy %s %s x%d @ %.2f", s.slug, side, qty, price)
                placed += 1
        return placed

    # --- one pass -----------------------------------------------------------
    def cycle(self) -> dict:
        started = _now()
        self._heartbeat({"phase": "start", "ts": started, "mode": self.mode})

        # Bookkeeping first: settle resolved markets even if trading is halted.
        try:
            settle_positions(self.cfg, self.state, self.kalshi, self.ledger, self.log)
        except Exception:  # noqa: BLE001
            self.log.exception("settlement check failed")

        # Pick dry vs live for THIS cycle from the arm switch + kill-switch.
        self.executor = self._select_executor()

        pre = self.risk.preflight()
        if not pre.allowed:
            self.log.warning("preflight blocked: %s", pre.reason)
            summary = {"phase": "blocked", "ts": started, "mode": self.mode,
                       "reason": pre.reason}
            self._heartbeat(summary)
            return summary

        reviewed = 0
        try:
            reviewed = review_open_orders(
                self.cfg, self.state, self.kalshi, self.odds, self.whales,
                self.executor, self.risk, self.log,
            )
        except Exception:  # noqa: BLE001 - review must never crash the cycle
            self.log.exception("order review failed")

        # All edge sources, ranked together by EV: sports de-vig, whale-primary
        # (politics/econ), and the crypto lognormal model (free data, no Odds API).
        results = find_opportunities(self.cfg, self.odds, self.kalshi, self.whales)
        whale_edges = find_whale_edges(self.cfg, self.kalshi, self.whales)
        crypto_edges = (find_crypto_edges(self.cfg, self.kalshi, self.crypto_data)
                        if self.crypto_data is not None else [])
        # (opp, fair_yes, label, force_paper, extra). Crypto stays paper until validated
        # (cfg.crypto_live) - it records for the backtest but never risks real money yet.
        # `extra` is per-sleeve metadata for the paper record; sports carries the
        # de-vig A/B fields (fair_mult / fair_power / fair_shin) on every ticket.
        crypto_paper = not self.cfg.crypto_live
        candidates = ([(r.opp, r.fair_yes, r.label, False, r.fair_all) for r in results]
                      + [(w.opp, w.signal.prob, f"whale: {w.label}", False, None)
                         for w in whale_edges]
                      + [(c.opp, c.model_prob, f"crypto: {c.asset}", crypto_paper, None)
                         for c in crypto_edges])
        candidates.sort(key=lambda c: c[0].expected_value, reverse=True)

        placed = 0
        for opp, fair_yes, label, force_paper, extra in candidates:
            if placed >= self.cfg.max_orders_per_cycle:
                break
            decision = self.risk.vet(opp)
            if not decision.allowed:
                self.log.info("skip %s: %s", opp.ticker, decision.reason)
                continue
            executor = self.dry_executor if force_paper else self.executor
            if executor.place(opp, fair_yes, note=label, extra=extra).ok:
                placed += 1

        # Polymarket US copy-trading (its own arm/enable gate inside).
        copies = 0
        try:
            copies = self._run_copy_trades()
        except Exception:  # noqa: BLE001
            self.log.exception("copy trades failed")

        if self.executor is self.live_executor and self.cfg.use_demo:
            self.state.set_meta("demo_run_ok", "1")  # a clean live-demo cycle unlocks prod

        summary = {
            "phase": "done", "ts": started, "mode": self.mode,
            "reviewed": reviewed, "candidates": len(candidates),
            "whale_edges": len(whale_edges), "crypto_edges": len(crypto_edges),
            "placed": placed, "copies": copies,
            "open_positions": self.state.open_position_count(),
            "exposure": round(self.state.open_exposure(), 2),
            "today_pnl": round(self.state.today_realized_pnl(), 2),
        }
        self._heartbeat(summary)
        self.log.info("cycle done: %s", summary)
        return summary

    def run_once(self) -> dict:
        return self.cycle()

    def run_forever(self) -> None:
        self.log.info("daemon start: interval=%.0fs", self.cfg.poll_interval)
        while True:
            try:
                self.cycle()
            except Exception:  # noqa: BLE001 - keep the daemon alive across failures
                self.log.exception("cycle crashed; continuing")
            time.sleep(self.cfg.poll_interval)

    # --- introspection ------------------------------------------------------
    def status(self) -> dict:
        hb = {}
        p = Path(self.cfg.heartbeat_path)
        if p.exists():
            try:
                hb = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                hb = {"error": "unreadable heartbeat"}
        return {
            "mode": self.mode,
            "live_capable": self.live_executor is not None,
            "armed": control.armed(self.cfg),
            "last_heartbeat": hb,
            "open_positions": self.state.open_position_count(),
            "exposure": round(self.state.open_exposure(), 2),
            "today_pnl": round(self.state.today_realized_pnl(), 2),
            "ledger": self.ledger.summary(),
        }

    def _heartbeat(self, payload: dict) -> None:
        p = Path(self.cfg.heartbeat_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({**payload, "written": _now()}, indent=2), encoding="utf-8")
