"""Ranked trade tickets for a human to place manually on Omen.

This is the research half of the bot with the execution half deliberately removed.
It takes smart-money flow off public Polymarket, applies the filters that the live
2026-06-26 session proved we needed, and emits ranked *suggestions* with the
reasoning attached so the decision stays with the person placing the trade.

What that live session taught us, and what each filter here answers:

  - All four copied trades were on matches already in their second half. Whales react
    to a goal faster than any polling loop, so we filled at the post-event price.
    -> ``in-play`` and ``resolves-too-soon`` filters.
  - The bot bought YES on one team and NO on the other in the same game, twice over,
    treating one opinion as four positions.
    -> ``duplicate-event`` filter keeps the single best ticket per event.
  - We copied fills that were already stale, at prices that had moved away from the
    whale's entry.
    -> ``stale`` and ``price-ran-away`` filters.

Every rejection is counted and reported, so the board can say "23 signals in, 3
tickets out, and here is where the other 20 went" instead of quietly showing three.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .copytrade import find_copy_signals
from .omen import OmenAccount, Sizing, event_url, size_position


@dataclass(frozen=True)
class TradeTicket:
    """One suggestion: what to buy, at what price, how much, and why."""

    market_id: str
    title: str
    side_label: str            # spelled out - "Türkiye to win", not "tur YES"
    outcome: str               # the raw Polymarket outcome name
    entry_price: float         # what you would pay now
    whale_price: float         # what the smart money paid
    drift_c: float             # cents the price has moved since they filled (+ = worse)
    sizing: Sizing
    conviction: float          # 0..1
    hours_to_resolve: float | None
    n_wallets: int
    whale_usd: float
    minutes_ago: float
    liquidity: float
    url: str
    # Absolutes, so a published snapshot can recompute "resolves in" at view time
    # rather than freezing the countdown at the moment it was generated.
    end_iso: str = ""
    signal_ts: float = 0.0
    why: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return self.sizing.cost

    @property
    def contracts(self) -> int:
        return self.sizing.contracts


@dataclass
class BoardReport:
    """Tickets plus an honest account of everything that was thrown away."""

    tickets: list[TradeTicket] = field(default_factory=list)
    considered: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


def _hours_until(iso: str, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - now).total_seconds() / 3600.0


def _max_signal_age(cfg, hours_to_resolve: float | None) -> float:
    """How stale a whale's fill may be, scaled to how long the market has left to run."""
    if hours_to_resolve is None:
        return cfg.board_max_signal_age_min
    scaled = hours_to_resolve * 60.0 * cfg.board_stale_fraction
    return min(cfg.board_max_signal_age_cap_min,
               max(cfg.board_max_signal_age_min, scaled))


def _side_label(outcome: str, title: str) -> str:
    """Say which side in words. 'LALBOS yes' is unreadable; 'Boston to win' is not."""
    o = (outcome or "").strip()
    if o.lower() in ("yes", "no"):
        return f"{o.upper()} on: {title}"
    return f"{o} — {title}"


def _conviction(*, n_wallets: int, usd: float, minutes_ago: float, drift_c: float,
                hours_to_resolve: float | None, liquidity: float) -> tuple[float, list[str]]:
    """Blend the signal's components into 0..1, and explain the blend in words.

    Deliberately simple and legible: every term is something you could check by hand.
    It ranks ideas against each other - it is not a probability of winning.
    """
    why: list[str] = []
    score = 0.0

    agree = min(1.0, n_wallets / 3.0)
    score += 0.30 * agree
    why.append(f"{n_wallets} profitable wallet{'s' if n_wallets != 1 else ''} on this side")

    # $10k is the floor to count at all; $200k+ is as convincing as size gets.
    size = min(1.0, (usd / 200_000.0) ** 0.5)
    score += 0.25 * size
    why.append(f"${usd:,.0f} of smart money behind it")

    fresh = max(0.0, 1.0 - minutes_ago / 180.0)
    score += 0.15 * fresh
    why.append(f"latest buy {minutes_ago:.0f} min ago")

    # Drift is what killed the June batch: paying up for someone else's information.
    undrifted = max(0.0, 1.0 - max(0.0, drift_c) / 6.0)
    score += 0.20 * undrifted
    if drift_c > 0.5:
        why.append(f"price up {drift_c:.1f}c since they bought")
    elif drift_c < -0.5:
        why.append(f"price {abs(drift_c):.1f}c cheaper than they paid")
    else:
        why.append("still near their entry price")

    room = 0.0 if hours_to_resolve is None else min(1.0, hours_to_resolve / 72.0)
    score += 0.10 * room
    if hours_to_resolve is not None:
        why.append(f"{hours_to_resolve:.0f}h until it resolves")

    if liquidity >= 50_000:
        why.append(f"${liquidity:,.0f} liquidity")

    return round(min(1.0, score), 3), why


def build_board(cfg, client, scorer, account: OmenAccount, *,
                now_ts: float | None = None, meta_fetch=None) -> BoardReport:
    """Whale flow -> filtered, ranked, human-placeable tickets."""
    from .gamma import market_meta, outcome_price

    report = BoardReport()
    now = time.time() if now_ts is None else now_ts
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)

    try:
        signals = find_copy_signals(
            client, scorer,
            categories=cfg.copytrade_categories,
            min_usd=cfg.copytrade_min_usd,
            min_wallets=cfg.copytrade_min_wallets,
            max_price=cfg.copytrade_max_price,
            limit=cfg.copytrade_feed_limit,
            time_periods=cfg.copytrade_time_periods,
            leaderboard_limit=cfg.copytrade_leaderboard_limit,
            now_ts=now,
        )
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"whale feed unavailable: {exc}")
        return report

    report.considered = len(signals)
    if not signals:
        report.notes.append("no qualifying smart-money buys in the current feed")
        return report

    fetcher = meta_fetch or market_meta
    try:
        metas = fetcher([s.market_id for s in signals])
    except Exception:  # noqa: BLE001
        metas = {}

    candidates: list[TradeTicket] = []
    for s in signals:
        m = metas.get(s.market_id)

        # Fail CLOSED on missing metadata. Without it there is no end date and no game
        # start, so every date filter below would silently pass - "we know nothing" must
        # never read as "nothing is wrong". A dropped metadata batch once let the whole
        # board through unchecked before dying at the pricing step.
        if not m:
            report.reject("no market metadata")
            continue

        if m.get("closed") or not m.get("active", True):
            report.reject("market closed")
            continue

        hours = _hours_until(m.get("end_date", ""), now_dt)
        if hours is not None and hours <= 0:
            report.reject("already resolved")
            continue

        start_h = _hours_until(m.get("game_start", ""), now_dt)
        if start_h is not None and start_h <= 0:
            report.reject("in-play (event already started)")
            continue

        if hours is not None and hours < cfg.board_min_hours:
            report.reject(f"resolves in under {cfg.board_min_hours:.0f}h")
            continue

        if hours is not None and hours > cfg.board_max_days * 24:
            report.reject(f"resolves more than {cfg.board_max_days:.0f} days out")
            continue

        if s.minutes_ago > _max_signal_age(cfg, hours):
            report.reject("signal too old for this market's horizon")
            continue

        # What we would pay for *the outcome the whale bought*. On a multi-outcome
        # market the YES book belongs to a different leg, so price the named outcome
        # explicitly and refuse the ticket rather than quote the wrong side.
        named = outcome_price(m, s.outcome)
        side = s.outcome.strip().lower()
        if side == "yes":
            entry = float(m.get("best_ask") or 0.0) or named or float(m.get("yes_price") or 0.0)
        elif side == "no":
            bid = float(m.get("best_bid") or 0.0)
            entry = round(1.0 - bid, 3) if bid else (named or 0.0)
        else:
            entry = named or 0.0
        if not entry:
            report.reject("could not price this outcome")
            continue
        if not (cfg.board_min_price <= entry <= cfg.board_max_price):
            report.reject("price outside tradeable band")
            continue

        drift_c = round((entry - s.avg_price) * 100, 2)
        if drift_c > cfg.board_max_drift_c:
            report.reject("price ran away from the whale's entry")
            continue

        conviction, why = _conviction(
            n_wallets=s.n_wallets, usd=s.total_usd, minutes_ago=s.minutes_ago,
            drift_c=drift_c, hours_to_resolve=hours,
            liquidity=float(m.get("liquidity") or 0.0),
        )
        if conviction < cfg.board_min_conviction:
            report.reject("conviction below threshold")
            continue

        sizing = size_position(entry, account, conviction=conviction)
        if sizing is None:
            report.reject("no valid position size")
            continue

        warnings: list[str] = []
        if float(m.get("liquidity") or 0.0) < 10_000:
            warnings.append("thin liquidity — expect slippage")
        if hours is not None and hours < 48:
            warnings.append("resolves within 2 days")
        if s.n_wallets == 1:
            warnings.append("only one wallet — single opinion")
        if sizing.capped_by == "omen-market-limit":
            warnings.append("size capped by Omen's per-market contract limit")

        candidates.append(TradeTicket(
            market_id=s.market_id,
            title=m.get("question") or s.title,
            side_label=_side_label(s.outcome, m.get("question") or s.title),
            outcome=s.outcome,
            entry_price=round(entry, 3),
            whale_price=round(s.avg_price, 3),
            drift_c=drift_c,
            sizing=sizing,
            conviction=conviction,
            hours_to_resolve=None if hours is None else round(hours, 1),
            n_wallets=s.n_wallets,
            whale_usd=round(s.total_usd),
            minutes_ago=round(s.minutes_ago),
            liquidity=round(float(m.get("liquidity") or 0.0)),
            url=event_url(s.event_slug) or event_url(m.get("slug", "")),
            end_iso=m.get("end_date", ""),
            signal_ts=now - s.minutes_ago * 60.0,
            why=why,
            warnings=warnings,
        ))

    # One opinion per event. Backing both teams in a game is one view, not two bets.
    best: dict[str, TradeTicket] = {}
    for t in sorted(candidates, key=lambda c: c.conviction, reverse=True):
        key = t.url or t.market_id
        if key in best:
            report.reject("duplicate event (kept the stronger side)")
            continue
        best[key] = t

    report.tickets = sorted(best.values(), key=lambda c: c.conviction, reverse=True)[
        : cfg.board_max_tickets
    ]
    if not report.tickets and not report.notes:
        report.notes.append(
            "Signals came in but none cleared the filters — that is a normal, and "
            "usually correct, outcome."
        )
    return report
