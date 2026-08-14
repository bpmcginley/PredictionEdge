"""Domain x horizon calibration overlay on Kalshi prices. A SANITY LAYER, not an edge.

Le (2026, arXiv 2602.19520) regressed outcome log-odds on price log-odds across 64.7M
Kalshi trades through Dec 2025 and found the calibration slope ``b`` varies by market
domain and time-to-expiry: b < 1 means the market is OVERconfident (true probability
sits closer to 50c than the price), b > 1 means UNDERconfident (favorites underpriced).
Headline slopes: weather 0.69-0.97 under 48h, sports single-game 0.90-1.10 (calibrated),
politics 0.93-1.83 and persistently underconfident at longer horizons, finance/crypto
~1.0, and EVERY domain compresses toward 50c beyond a month (favorite-longshot bias).

What this module does with that: map (price, domain, hours_to_expiry) to a calibrated
probability via logit(p') = b_eff * logit(price), and let a sleeve ask "once the
market's known miscalibration is corrected out of its price, does my edge survive?"
If the corrected price already agrees with the model, the "edge" was most likely the
market being right in a way the model cannot see - so the ticket is suppressed rather
than sized. Prices are never edited; fair values are never edited; the overlay only
vetoes and annotates.

Two deliberate conservatisms, because the study window ended Dec 2025 and slopes drift:

  1. ``shrink`` (config ``calibration_shrink``, default 0.5) applies only HALF the
     published correction: b_eff = 1 + shrink * (b_table - 1). shrink=0 turns the
     whole overlay into an identity map.
  2. The table encodes the MIDDLE of each published range, and any domain x bucket
     the paper did not measure gets 1.0 - no data, no correction. Sports and finance
     are 1.0 exactly at every sub-month horizon so the overlay is a structural no-op
     there, not merely a small one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_SHRINK = 0.5

# Output clamp. A calibration slope is a population-level statement; letting it push
# an individual market past 99/1 would claim more precision than the regression has.
PROB_FLOOR = 0.01
PROB_CEIL = 0.99

# Horizon bucket edges, in hours. A boundary value belongs to the LONGER bucket
# (48h exactly is "48h-1wk"), matching how the paper bins half-open intervals.
_H48 = 48.0
_WEEK = 7 * 24.0
_MONTH = 30 * 24.0

BUCKETS = ("<48h", "48h-1wk", "1wk-1mo", ">1mo")

# The >1mo compression toward 50c is reported for ALL domains but without a per-domain
# range, so it is encoded once, mildly, rather than guessed per-domain.
_LONG_HORIZON_B = 0.90

# b by domain, one value per bucket in BUCKETS order. Encoding choices, so nobody has
# to re-derive them: weather <48h is the middle of the published 0.69-0.97 (0.83);
# politics "longer horizons" is the middle of 0.93-1.83 (1.38) applied to the two
# measured longer sub-month buckets, with <48h left at 1.0 (the range's low end is
# ~calibrated and near-resolution politics gives no license to correct); sports and
# finance/crypto are exactly 1.0 sub-month per the paper.
B_TABLE: dict[str, tuple[float, float, float, float]] = {
    "weather":  (0.83, 1.00, 1.00, _LONG_HORIZON_B),
    "sports":   (1.00, 1.00, 1.00, _LONG_HORIZON_B),
    "politics": (1.00, 1.38, 1.38, _LONG_HORIZON_B),
    "finance":  (1.00, 1.00, 1.00, _LONG_HORIZON_B),
    "crypto":   (1.00, 1.00, 1.00, _LONG_HORIZON_B),
}

# An unknown domain gets the identity everywhere. Correcting a market the study never
# measured would be inventing a miscalibration, which is worse than missing one.
_IDENTITY = (1.00, 1.00, 1.00, 1.00)


def horizon_bucket(hours_to_expiry: float) -> int:
    """Index into BUCKETS. Negative or NaN hours read as "resolving now" (bucket 0)."""
    h = hours_to_expiry
    if math.isnan(h) or h < _H48:
        return 0
    if h < _WEEK:
        return 1
    if h < _MONTH:
        return 2
    return 3


def table_b(domain: str, hours_to_expiry: float) -> float:
    """The published (un-shrunk) slope for this domain and horizon."""
    row = B_TABLE.get((domain or "").strip().lower(), _IDENTITY)
    return row[horizon_bucket(hours_to_expiry)]


def effective_b(domain: str, hours_to_expiry: float,
                shrink: float = DEFAULT_SHRINK) -> float:
    """The slope actually applied: 1 + shrink * (b_table - 1)."""
    return 1.0 + shrink * (table_b(domain, hours_to_expiry) - 1.0)


def calibrated_prob(price: float, domain: str, hours_to_expiry: float,
                    *, shrink: float = DEFAULT_SHRINK) -> float:
    """Miscalibration-corrected probability estimate for a quoted price.

    logit(p') = b_eff * logit(price), with a = 0: the study's intercepts are small and
    an intercept is exactly the kind of parameter that drifts first. b_eff < 1 pulls
    extreme prices toward 0.5 and leaves 0.5 fixed; b_eff = 1 is the identity (up to
    the clamps). Prices at or beyond 0/1 are clamped rather than rejected - a 0c or
    100c quote is a real thing a stale book serves, and this function's callers need
    an answer, not an exception.
    """
    p = min(max(float(price), 1e-9), 1.0 - 1e-9)
    b = effective_b(domain, hours_to_expiry, shrink)
    logit = math.log(p / (1.0 - p))
    out = 1.0 / (1.0 + math.exp(-b * logit))
    return min(max(out, PROB_FLOOR), PROB_CEIL)


@dataclass(frozen=True)
class Assessment:
    """The overlay's verdict on one candidate ticket."""
    ok: bool             # False = suppress the ticket
    cal_prob: float      # calibrated probability for the quoted price
    cal_edge: float      # fair_prob - cal_prob
    cal_b: float         # the effective (shrunk) slope that produced cal_prob
    reason: str = ""     # why a veto fired; "" when ok


def assess(fair_prob: float, price: float, domain: str, hours_to_expiry: float,
           *, shrink: float = DEFAULT_SHRINK,
           min_abs_edge: float = 0.0) -> Assessment:
    """Should a sleeve's edge claim survive the market's known miscalibration?

    The sleeve computed edge = fair - price. This recomputes it against the calibrated
    price, and vetoes when the two disagree in SIGN (the corrected market already sits
    on the model's side of the quote - the "edge" is the miscalibration itself, priced
    backwards) or when |cal_edge| falls under ``min_abs_edge`` (typically the fee: an
    edge the correction shrinks below cost was never an edge). Agreement passes, and
    the caller records both numbers so the trial can score the veto rule later.
    """
    cal_prob = calibrated_prob(price, domain, hours_to_expiry, shrink=shrink)
    b = effective_b(domain, hours_to_expiry, shrink)
    edge = fair_prob - price
    cal_edge = fair_prob - cal_prob
    if edge * cal_edge <= 0.0:
        return Assessment(False, cal_prob, cal_edge, b,
                          "calibrated price agrees with the model")
    if abs(cal_edge) < min_abs_edge:
        return Assessment(False, cal_prob, cal_edge, b,
                          "calibrated edge below fee threshold")
    return Assessment(True, cal_prob, cal_edge, b)
