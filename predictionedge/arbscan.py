"""Kalshi <-> Polymarket-US cross-venue gap scanner — FRAMEWORK (WS-D #1).

Structural edge: the same binary event priced on both venues. Buy YES on the
cheap venue, YES-equivalent hedge (NO) on the rich one; if combined cost after
fees < $1.00 per contract-pair, the payoff is locked regardless of outcome.
No forecast needed — the risk is operational, not directional:

1. RESOLUTION MISMATCH is the real killer. "Same" event can settle against
   different sources or definitions (e.g. different cutoff times, different
   official data series). A pair is only tradable after its two rule texts are
   verified equivalent — hence MANUAL_VERIFIED gating below, never fuzzy-match
   straight to live orders.
2. Capital locks until resolution: a 2c gross gap on a 90-day market is ~4%
   over the lock, not 4%/trade. min_annualized_return filters for this.
3. Windows are ~30 seconds on liquid events (competing bots), so the scanner
   separates SLOW discovery (matching, done offline) from FAST evaluation
   (BBO reads on known pairs, every cycle).

Wiring status: gap math + gating are implemented and unit-testable. Venue
scanning, pair persistence, and order legs are TODO stubs. Paper-trades first
per WS-F: tag orders strategy="arbscan", promote only on a positive paper
record (>= 20 samples).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .fees import fee_per_contract

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ArbScanConfig:
    min_net_edge_per_pair: float = 0.015   # $ per contract-pair after all fees
    min_annualized_return: float = 0.15    # net edge annualized over capital lock
    max_stake_per_pair: float = 50.0       # $ combined cost basis per pair
    max_days_to_resolution: float = 45.0   # beyond this the lock isn't worth it
    max_quote_age_s: float = 10.0          # both BBOs must be this fresh
    # Polymarket-US fee model. TODO(wire-up): verify current PM-US fee schedule
    # and replace this flat approximation (Kalshi side uses fees.py exactly).
    pmus_fee_per_contract: float = 0.0
    dry_run: bool = True


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

class PairStatus(Enum):
    CANDIDATE = "candidate"          # fuzzy-matched, rules NOT compared
    MANUAL_VERIFIED = "verified"     # human read both rule texts; tradable
    REJECTED = "rejected"            # rules differ; never trade, keep to avoid re-flagging


@dataclass(slots=True)
class VenuePair:
    """One event listed on both venues, with verification state.

    Persist these (state.db table `arb_pairs`) so verification survives
    restarts and rejected pairs stay rejected.
    """

    kalshi_ticker: str
    pmus_market_id: str
    description: str
    status: PairStatus = PairStatus.CANDIDATE
    resolution_utc: datetime | None = None
    notes: str = ""  # why verified/rejected — future-you will want this


@dataclass(slots=True)
class GapQuote:
    """Fresh BBOs for one pair, ready for edge evaluation."""

    pair: VenuePair
    kalshi_yes_ask: float | None
    kalshi_no_ask: float | None
    pmus_yes_ask: float | None
    pmus_no_ask: float | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class GapOpportunity:
    """A tradable (or paper-tradable) locked pair, fully costed."""

    pair: VenuePair
    buy_yes_venue: str            # "kalshi" | "pmus"
    yes_price: float
    no_price: float
    fees_per_pair: float
    net_edge_per_pair: float      # $1.00 payoff minus all-in cost
    days_locked: float
    annualized_return: float


# --------------------------------------------------------------------------
# Pure logic — implemented
# --------------------------------------------------------------------------

def evaluate_gap(
    cfg: ArbScanConfig, q: GapQuote, now: datetime
) -> GapOpportunity | None:
    """Return the better locked combo for this pair if it clears every gate."""
    if q.pair.status is not PairStatus.MANUAL_VERIFIED:
        return None
    if (now - q.fetched_at).total_seconds() > cfg.max_quote_age_s:
        return None
    if q.pair.resolution_utc is None:
        return None
    days = (q.pair.resolution_utc - now).total_seconds() / 86400
    if not 0 < days <= cfg.max_days_to_resolution:
        return None

    combos = []
    # YES on Kalshi (taker) + NO on PM-US, and the mirror.
    if q.kalshi_yes_ask is not None and q.pmus_no_ask is not None:
        fees = fee_per_contract(q.kalshi_yes_ask) + cfg.pmus_fee_per_contract
        combos.append(("kalshi", q.kalshi_yes_ask, q.pmus_no_ask, fees))
    if q.pmus_yes_ask is not None and q.kalshi_no_ask is not None:
        fees = fee_per_contract(q.kalshi_no_ask) + cfg.pmus_fee_per_contract
        combos.append(("pmus", q.pmus_yes_ask, q.kalshi_no_ask, fees))

    best: GapOpportunity | None = None
    for venue, yes_p, no_p, fees in combos:
        net = 1.0 - (yes_p + no_p) - fees
        if net < cfg.min_net_edge_per_pair:
            continue
        cost = yes_p + no_p + fees
        ann = (net / cost) * (365 / days)
        if ann < cfg.min_annualized_return:
            continue
        opp = GapOpportunity(q.pair, venue, yes_p, no_p, fees, net, days, ann)
        if best is None or opp.net_edge_per_pair > best.net_edge_per_pair:
            best = opp
    return best


def size_pairs(cfg: ArbScanConfig, opp: GapOpportunity, budget: float) -> int:
    """Contract-pairs to trade given remaining exposure budget."""
    cost_per_pair = opp.yes_price + opp.no_price + opp.fees_per_pair
    if cost_per_pair <= 0:
        return 0
    cap = min(cfg.max_stake_per_pair, budget)
    return int(cap // cost_per_pair)


# --------------------------------------------------------------------------
# Orchestration skeleton
# --------------------------------------------------------------------------

class ArbScanner:
    """Discovery (slow, offline) and evaluation (fast, per-cycle) split.

    Integration points (all TODO for wire-up):
    - discovery: list active markets on both venues; entity/date extraction +
      fuzzy title match (extend matching.py beyond sports) -> CANDIDATE pairs
      into state.db; dashboard tile lists candidates awaiting manual rule
      verification (a human promotes to MANUAL_VERIFIED with one click).
    - evaluation: for each verified pair, fetch both BBOs (existing kalshi.py
      + polymarket_us.py clients), evaluate_gap(), then paper/live legs via
      executor.py with strategy="arbscan". Legging risk: place the scarcer
      side first; if the second leg misses, unwind immediately at taker.
    - risk: RiskManager.vet() both legs; respect per-strategy sub-caps (WS-H).
    """

    def __init__(self, cfg: ArbScanConfig):
        self.cfg = cfg

    def discover_pairs(self) -> list[VenuePair]:
        """TODO(wire-up): cross-venue fuzzy matching -> CANDIDATE pairs."""
        raise NotImplementedError("wire-up: cross-venue market matching")

    def fetch_quotes(self, pairs: list[VenuePair]) -> list[GapQuote]:
        """TODO(wire-up): concurrent BBO reads from both venue clients."""
        raise NotImplementedError("wire-up: BBO fetch")

    def cycle(self, budget: float) -> list[GapOpportunity]:
        """TODO(wire-up): fetch -> evaluate -> size -> paper/live execution."""
        raise NotImplementedError("wire-up: scan cycle")
