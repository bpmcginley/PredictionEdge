"""E3 - Macro fair value: free references that genuinely beat retail guesswork.

Two independent reference prices for economic markets on global Polymarket:

1. **CPI <- Cleveland Fed inflation nowcast.** SHIPS. Published daily, free, no key,
   and materially better than what a retail crowd produces from headlines.
2. **Fed decisions <- fed funds futures.** MATH SHIPS, FEED DOES NOT. See the
   verification log below: there is no free, no-key, terms-compliant source of fed
   funds futures prices. Per EDGE_PLAN.md that is a *finding*, not something to route
   around with a scrape, so the futures->probability maths is built and tested and the
   live leg fails closed until someone supplies a price.

-------------------------------------------------------------------------------
SOURCE VERIFICATION LOG  (probed live 2026-08-06 from this machine)
-------------------------------------------------------------------------------
REACHABLE, FREE, NO KEY:
  * Cleveland Fed inflation nowcast (the CPI reference this module runs on)
      https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/
          nowcast_month.json   -> month-over-month, seasonally adjusted
          nowcast_year.json    -> year-over-year, NOT seasonally adjusted
          nowcast_quarter.json -> quarterly annualised (unused; no PM market matches)
      HTTP 200. This is the data file backing their own published chart, served with
      an export-data menu item enabled - i.e. the intended public route, not a scrape.
      Carries 158 monthly vintages back to 2013-07, each with the daily nowcast path
      AND the eventual actual print, which is what let us measure sigma empirically
      (see _MEASURED_NOWCAST_RMSE_PP).
  * NY Fed reference rates (current FOMC target range, for the Fed maths)
      https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json
      HTTP 200, JSON, gives EFFR plus targetRateFrom/targetRateTo.
  * Polymarket Gamma (the venue we must be able to place on)
      https://gamma-api.polymarket.com/public-search  and  /events?slug=...

NOT USABLE - reported rather than worked around:
  * CME Group quote endpoints (the actual fed funds futures strip, product 305)
      https://www.cmegroup.com/CmeWS/mvc/Quotes/Future/305/G
      -> HTTP 403 with an explicit body: this IP is blocked for "suspected web
         scraping" and use of "scripts, software, spiders, robots ... or other
         scraping mechanisms" is prohibited. That is a stated terms prohibition on
         top of a technical block. HARD STOP - we do not go around it.
  * FRED (the documented fallback host)
      https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFF
      -> unreachable from this machine: 3 consecutive 60s read timeouts and one
         connection reset. Note this is a *network* failure, not an auth failure, so
         it may work elsewhere. It would not rescue the Fed leg regardless: FRED
         carries realised rates (DFF/EFFR/target range), not CME futures settlement
         prices, which it does not have redistribution rights to. Realised rates are
         not expectations.
  * Atlanta Fed Market Probability Tracker - REJECTED ON CLAIM MISMATCH, not access.
      https://www.atlantafed.org/research-and-data/data/market-probability-tracker
      mpt_histdata.xlsx downloads fine (HTTP 200, no key). It looks like the obvious
      substitute and it is not. Its data dictionary says the distributions are over
      "average SOFR over the reference window" for "the 3-month window referenced by
      CME 3-month SOFR contracts". That is a different object from "the target range
      the FOMC sets on 2026-09-16": it is SOFR not EFFR (a basis), and a 3-month
      average that can span more than one meeting (a decomposition). Turning it into
      per-meeting cut probabilities needs two unpinned assumptions, which is exactly
      the machinery that manufactures fake edges. Using it would also cost a 6.6MB
      download and an openpyxl dependency this repo does not carry.

-------------------------------------------------------------------------------
WHY THE CLAIM MATTERS MORE THAN THE HEADLINE
-------------------------------------------------------------------------------
`crossvenue.py` burned this project by scoring two questions as the same because the
words matched. The macro version of that mistake is subtler and worse, because the
numbers look authoritative:

  * Polymarket CPI brackets resolve on the BLS *printed, one-decimal* figure. "0.2%"
    does not mean 0.2 exactly, it means the print rounds to 0.2 - the bin is
    [0.15, 0.25). Comparing a nowcast point estimate to the label as if it were a
    point is how you get a confident, wrong answer. See `bracket_bounds`.
  * Seasonal adjustment is part of the claim. The MoM markets say "seasonally
    adjusted change from the preceding month"; the YoY markets say "before seasonal
    adjustment". Those map to *different* Cleveland Fed files. Crossing them is a
    silent 20-40bp error. See `_SERIES_BASIS`.
  * Fed markets partition as {50+ cut, 25 cut, no change, 25 hike, 50+ hike}. The
    "50+" legs are open-ended tails. A two-point futures decomposition cannot express
    mass out there at all, so it must refuse rather than report zero. See
    `one_meeting_outcome_probs`.

All reads here are public and unauthenticated. Advisory only - this module prints,
it never trades, and it constructs no executor.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from statistics import NormalDist
from typing import Iterable

from .config import Config

GAMMA = "https://gamma-api.polymarket.com"
CLEVELAND_NOWCAST = (
    "https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_{kind}.json"
)
NYFED_EFFR = "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json"

_ND = NormalDist()

# BLS prints CPI to one decimal place, so every Polymarket bracket is a *rounding bin*
# half a tick wide on each side. This is the single most important constant in the
# CPI path and the easiest one to get silently wrong.
_PRINT_TICK_PP = 0.1
_HALF_TICK_PP = _PRINT_TICK_PP / 2.0

# Measured, not assumed. Across all 154 Cleveland Fed vintages that have since printed
# (2013-07 .. 2026-06), the error of the FINAL nowcast against the eventual actual is:
#     CPI MoM       bias -0.009  rmse 0.150
#     Core CPI MoM  bias -0.001  rmse 0.156
#     CPI YoY       bias -0.010  rmse 0.159
#     Core CPI YoY  bias -0.001  rmse 0.162
# Essentially unbiased, so a symmetric normal centred on the nowcast is the honest
# shape; the spread is ~0.16pp at the horizon that matters (the last nowcast before
# the print). `cfg.macro_cpi_nowcast_sigma` currently defaults to 0.12, which is
# tighter than the source's own track record and would make every bracket probability
# overconfident. We therefore take the WIDER of config and this floor - widening can
# only shrink claimed edges, never invent them. Revisit by re-running the measurement
# in `scripts/` after any Cleveland Fed methodology change, or if the config value is
# ever raised above this floor (at which point config wins and this stops binding).
_MEASURED_NOWCAST_RMSE_PP = 0.16

# Cleveland Fed series name -> (what Polymarket calls it, which file/basis it lives in).
# The basis is load-bearing: MoM is seasonally adjusted, YoY is not, and the two live
# in different JSON files. Getting this pairing wrong is a silent error, not a crash.
_SERIES_BASIS = {
    ("cpi", "mom"): ("CPI Inflation", "month"),
    ("cpi", "yoy"): ("CPI Inflation", "year"),
    ("core_cpi", "mom"): ("Core CPI Inflation", "month"),
    ("core_cpi", "yoy"): ("Core CPI Inflation", "year"),
}

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")


# ---------------------------------------------------------------------------
# Fed funds futures -> probability.  Maths only; see the log above for the feed.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MeetingDecomposition:
    """A single FOMC meeting backed out of one 30-day fed funds futures contract."""

    implied_average: float      # % - the contract's implied average EFFR for the month
    rate_before: float          # % - effective rate in force entering the month
    rate_after: float           # % - implied effective rate after the meeting
    days_before: int
    days_in_month: int


def ff_implied_average_rate(futures_price: float) -> float:
    """Implied average EFFR for the contract month, from a 30-day FF future.

    ZQ settles to 100 minus the arithmetic average of the daily effective fed funds
    rate over the delivery month. That identity is the whole basis of the calculation.
    """
    if not (0.0 < futures_price <= 100.0):
        raise ValueError(f"fed funds futures price out of range: {futures_price}")
    return 100.0 - futures_price


def decompose_meeting(futures_price: float, *, rate_before: float,
                      meeting_day: int, days_in_month: int) -> MeetingDecomposition:
    """Split a contract-month average into the pre- and post-meeting rate.

    An FOMC decision takes effect the day *after* the meeting, so a meeting on day
    ``meeting_day`` leaves that many days of the month at the old rate.
    """
    if days_in_month <= 0:
        raise ValueError("days_in_month must be positive")
    if not (1 <= meeting_day <= days_in_month):
        raise ValueError(f"meeting_day {meeting_day} outside month of {days_in_month} days")
    if meeting_day == days_in_month:
        # The decision lands entirely outside the contract month, so this contract
        # carries no information about it. Refuse rather than divide by zero.
        raise ValueError("meeting on the last day of the month: this contract cannot price it")
    avg = ff_implied_average_rate(futures_price)
    days_after = days_in_month - meeting_day
    after = (avg * days_in_month - rate_before * meeting_day) / days_after
    return MeetingDecomposition(avg, rate_before, after, meeting_day, days_in_month)


def two_point_move_probs(rate_after: float, rate_before: float, *,
                         step: float = 0.25) -> tuple[float, float] | None:
    """P(no move) and P(one ``step`` move) implied by the post-meeting rate.

    Returns ``(p_hold, p_move)`` or **None** when the implied rate sits outside the
    one-step window. None is the important branch: a rate implying more than one step
    means the market is pricing 50bp+, and a two-outcome decomposition physically
    cannot represent that. Reporting the clipped answer would understate the tail and
    manufacture an edge against the "50+ bps" leg.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    delta = rate_after - rate_before
    if abs(delta) > step + 1e-9:
        return None                      # multi-step move priced: not expressible here
    p_move = abs(delta) / step
    if not (-1e-9 <= p_move <= 1.0 + 1e-9):
        return None
    p_move = min(1.0, max(0.0, p_move))
    return (1.0 - p_move, p_move)


def one_meeting_outcome_probs(rate_after: float, rate_before: float, *,
                              step: float = 0.25) -> dict[str, float] | None:
    """Map a decomposed rate onto Polymarket's 5-way Fed partition, or refuse.

    Polymarket splits a meeting into {50+ bps decrease, 25 bps decrease, no change,
    25 bps increase, 50+ bps increase}. A single futures contract yields one number,
    which pins a two-point distribution only. That is enough to fill the middle three
    buckets when the implied move is within one step - and it is *not* enough when it
    is not, so we return None instead of asserting 0.0 into the open-ended tails.
    """
    split = two_point_move_probs(rate_after, rate_before, step=step)
    if split is None:
        return None
    p_hold, p_move = split
    cutting = rate_after < rate_before
    return {
        "50+ bps decrease": 0.0,
        "25 bps decrease": p_move if cutting else 0.0,
        "no change": p_hold,
        "25 bps increase": 0.0 if cutting else p_move,
        "50+ bps increase": 0.0,
    }


def fetch_target_range(*, fetch=None) -> tuple[float, float] | None:
    """Current FOMC target range (low, high) in percent, from the NY Fed. None on failure."""
    try:
        data = (fetch or _get)(NYFED_EFFR, None)
        row = (data.get("refRates") or [])[0]
        lo, hi = float(row["targetRateFrom"]), float(row["targetRateTo"])
    except Exception:  # noqa: BLE001 - fail closed; a missing target range prices nothing
        return None
    return (lo, hi)


# ---------------------------------------------------------------------------
# Cleveland Fed nowcast
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Nowcast:
    series: str          # "cpi" | "core_cpi"
    basis: str           # "mom" | "yoy"
    ref_month: str       # "YYYY-M" as the Cleveland Fed labels it
    value_pp: float      # percentage points
    as_of: str
    printed: bool        # True once the actual has been published for this vintage


def _parse_nowcast_payload(payload: list, series_name: str) -> dict[str, Nowcast]:
    """Pull the latest nowcast per reference month out of one chart JSON file.

    The file is a FusionCharts payload: a list of per-reference-month charts, each
    with a daily category axis and one data series per measure. We want, for each
    vintage, the last non-empty nowcast value and whether the actual has landed yet.
    """
    out: dict[str, Nowcast] = {}
    for chart in payload or []:
        meta = chart.get("chart") or {}
        ref = str(meta.get("subcaption") or "").strip()
        if not ref:
            continue
        by_name = {s.get("seriesname"): s.get("data") or [] for s in chart.get("dataset") or []}
        vals = [v.get("value") for v in by_name.get(series_name, [])]
        live = [v for v in vals if v not in ("", None)]
        actual = [v for v in by_name.get(f"Actual {series_name}", []) if v.get("value") not in ("", None)]
        if not live:
            continue
        try:
            value = float(live[-1])
        except (TypeError, ValueError):
            continue
        out[ref] = Nowcast(series="", basis="", ref_month=ref, value_pp=value,
                           as_of=str(meta.get("_comment") or ""), printed=bool(actual))
    return out


def fetch_nowcasts(series: str, basis: str, *, fetch=None) -> dict[str, Nowcast]:
    """All reference-month nowcasts for one series/basis, keyed "YYYY-M". {} on failure."""
    key = _SERIES_BASIS.get((series, basis))
    if key is None:
        return {}
    series_name, kind = key
    try:
        payload = (fetch or _get)(CLEVELAND_NOWCAST.format(kind=kind), None)
    except Exception:  # noqa: BLE001 - fail closed
        return {}
    raw = _parse_nowcast_payload(payload, series_name)
    return {ref: Nowcast(series, basis, nc.ref_month, nc.value_pp, nc.as_of, nc.printed)
            for ref, nc in raw.items()}


def nowcast_for(nowcasts: dict[str, Nowcast], ref_month: str) -> Nowcast | None:
    """The nowcast for one reference month, only while it is still unprinted.

    Once the actual is out the nowcast is worthless as a forecast, and any market
    still open against it is resolving on the print, not on our estimate. Returning
    None there is the fail-closed choice.
    """
    nc = nowcasts.get(ref_month)
    if nc is None or nc.printed:
        return None
    return nc


# ---------------------------------------------------------------------------
# Bracket maths
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bracket:
    label: str
    lo: float           # inclusive lower edge in pp (-inf for the bottom tail)
    hi: float           # exclusive upper edge in pp (+inf for the top tail)
    market_prob: float
    question: str
    condition_id: str
    volume: float


_NUM = r"-?\d+(?:\.\d+)?"


def bracket_bounds(label: str) -> tuple[float, float] | None:
    """Turn a Polymarket bracket label into the interval of *true* values it wins on.

    The labels quote the printed figure, which BLS rounds to one decimal. So the bin
    for "0.2%" is [0.15, 0.25), not the point 0.2, and the tails open half a tick past
    their stated edge. Handles the four shapes Polymarket actually uses today:
    "<=0.0%" / "0.1%" / "0.6%+" / ">=3.1%", plus the ASCII spellings.
    """
    if not label:
        return None
    s = label.strip().replace("≤", "<=").replace("≥", ">=")
    s = s.replace("or less", "<=").replace("or more", ">=").lower()
    m = re.search(_NUM, s)
    if m is None:
        return None
    v = float(m.group(0))
    lower_tail = "<=" in s
    upper_tail = ">=" in s or s.rstrip().endswith("+")
    if lower_tail and upper_tail:
        return None                      # contradictory label: refuse to guess
    if lower_tail:
        return (-math.inf, v + _HALF_TICK_PP)
    if upper_tail:
        return (v - _HALF_TICK_PP, math.inf)
    return (v - _HALF_TICK_PP, v + _HALF_TICK_PP)


def bracket_prob(lo: float, hi: float, mu: float, sigma: float) -> float:
    """P(lo <= X < hi) for X ~ Normal(mu, sigma)."""
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    top = 1.0 if hi == math.inf else _ND.cdf((hi - mu) / sigma)
    bot = 0.0 if lo == -math.inf else _ND.cdf((lo - mu) / sigma)
    return max(0.0, top - bot)


def partition_is_sound(brackets: Iterable[Bracket], *, tol: float = 1e-6) -> bool:
    """True when the brackets tile the real line with no gap and no overlap.

    This is the guard that catches a label-parse failure before it becomes a fake
    edge. A negRisk set is by construction mutually exclusive and exhaustive; if our
    parsed edges do not reproduce that, we misread something and must not price it.
    """
    bs = sorted(brackets, key=lambda b: b.lo)
    if len(bs) < 2:
        return False
    if bs[0].lo != -math.inf or bs[-1].hi != math.inf:
        return False
    for a, b in zip(bs, bs[1:]):
        if abs(a.hi - b.lo) > tol:
            return False
    return True


# ---------------------------------------------------------------------------
# Matching Polymarket events to a nowcast series
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CpiMatch:
    slug: str
    title: str
    series: str
    basis: str
    ref_month: str
    brackets: list[Bracket]


def classify_cpi_event(title: str, description: str = "") -> tuple[str, str, str] | None:
    """Identify (series, basis, ref_month) for a CPI-ish event, or None to skip it.

    Deliberately strict. Every one of these checks exists because getting it wrong
    produces a plausible-looking number rather than an error:

      * US only. "Japan Core CPI YoY", "Brazil Annual Inflation" and friends share
        almost every word with the US markets and the Cleveland Fed nowcasts none of
        them.
      * core vs headline decides which series.
      * monthly vs annual decides which *file*, and with it the seasonal-adjustment
        basis. The description is authoritative here ("seasonally adjusted change from
        the preceding month" vs "before seasonal adjustment").
      * The reference month must be explicit. An event we cannot date is not matchable.
    """
    t = (title or "").lower()
    d = (description or "").lower()
    blob = f"{t} {d}"

    # Reject anything naming a non-US economy before looking at anything else.
    foreign = ("japan", "china", "u.k.", "uk ", "united kingdom", "brazil", "korea",
               "eurozone", "euro area", "mexico", "canada", "argentina", "south africa",
               "india", "turkey", "russia", "australia")
    if any(f in t for f in foreign):
        return None
    if "cpi" not in blob and "inflation" not in blob:
        return None
    # "BLS delays another CPI release before 2027?" is a market about the publication
    # schedule, not about the number. Same words, different question.
    if "delay" in t or "postpone" in t:
        return None

    series = "core_cpi" if ("core" in t or "ex food and energy" in t) else "cpi"

    if "mom" in t or "monthly" in t or "month-over-month" in t or "one-month" in d:
        basis = "mom"
    elif "yoy" in t or "annual" in t or "year-over-year" in t or "12-month" in d:
        basis = "yoy"
    else:
        return None

    ref = _ref_month_from(title, description)
    if ref is None:
        return None
    return (series, basis, ref)


def _ref_month_from(title: str, description: str) -> str | None:
    """Extract the CPI reference month as "YYYY-M". None when it is not unambiguous.

    A year is always required - a bare "July" is ambiguous across vintages and would
    silently pick the wrong nowcast. Titles carry the reference month directly
    ("Core CPI YoY - July 2026"), so they are trusted as-is.

    Descriptions are NOT, because they also name the *release* date, which is a
    different month ("the July 2026 report ... released on August 12, 2026"). So in
    the description we only accept a month-year introduced by a word that marks it as
    the reference period, and if two such mentions disagree we return None rather than
    take the first one. Scanning descriptions naively in calendar order happens to get
    July/August right and would get December/January exactly backwards.
    """
    low_title = (title or "").lower()
    for idx, name in enumerate(_MONTHS, start=1):
        if re.search(rf"\b{name}\s+(\d{{4}})\b", low_title):
            return f"{re.search(rf'\b{name}\s+(\d{{4}})\b', low_title).group(1)}-{idx}"

    low_desc = (description or "").lower()
    found: set[str] = set()
    for idx, name in enumerate(_MONTHS, start=1):
        for m in re.finditer(rf"\b(?:ending|ending in|in|for|during)\s+{name}\s+(\d{{4}})\b", low_desc):
            found.add(f"{m.group(1)}-{idx}")
    if len(found) == 1:
        return found.pop()
    return None


def fetch_cpi_events(*, queries=("CPI", "inflation"), fetch=None) -> list[dict]:
    """Candidate live Polymarket events for CPI, via Gamma's public search.

    Uses /public-search rather than /markets?tag_slug=...: Gamma *silently ignores*
    tag_slug on the /markets route and hands back an unfiltered page, which looks like
    a working filter and is not. Verified 2026-08-06.
    """
    out: list[dict] = []
    seen: set[str] = set()
    getter = fetch or _get
    for q in queries:
        try:
            data = getter(f"{GAMMA}/public-search", {"q": q, "limit_per_type": 20})
        except Exception:  # noqa: BLE001 - one failed query must not sink the scan
            continue
        for ev in (data.get("events") or []):
            slug = ev.get("slug") or ""
            if slug and slug not in seen:
                seen.add(slug)
                out.append(ev)
    return out


def build_brackets(event: dict) -> list[Bracket]:
    """Parsed, priced brackets for one negRisk event. Empty when anything is unreadable."""
    out: list[Bracket] = []
    for m in event.get("markets") or []:
        if m.get("closed") or not m.get("active", True):
            continue
        label = m.get("groupItemTitle") or ""
        bounds = bracket_bounds(label)
        if bounds is None:
            return []                    # one unreadable label invalidates the partition
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:  # noqa: BLE001
                prices = None
        if not prices:
            return []                    # an unpriceable leg is a rejection, not a zero
        out.append(Bracket(
            label=label, lo=bounds[0], hi=bounds[1],
            market_prob=float(prices[0]),
            question=m.get("question") or "",
            condition_id=m.get("conditionId") or m.get("condition_id") or "",
            volume=float(m.get("volume") or 0.0),
        ))
    return out


def match_cpi_events(events: Iterable[dict]) -> list[CpiMatch]:
    """Classified, bracket-parsed CPI events that survive every guard."""
    out: list[CpiMatch] = []
    for ev in events:
        markets = ev.get("markets") or []
        desc = (markets[0].get("description") if markets else "") or ""
        cls = classify_cpi_event(ev.get("title") or "", desc)
        if cls is None:
            continue
        brackets = build_brackets(ev)
        if not brackets or not partition_is_sound(brackets):
            continue                     # misparsed ladder: refuse, do not price
        series, basis, ref = cls
        out.append(CpiMatch(slug=ev.get("slug") or "", title=ev.get("title") or "",
                            series=series, basis=basis, ref_month=ref, brackets=brackets))
    return out


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BracketCompare:
    bracket: Bracket
    model_prob: float
    gap_c: float         # (model - market) in cents; positive => market looks cheap
    suspect: bool        # gap beyond cfg.macro_max_gap_c: smells like a bad match


@dataclass(frozen=True)
class CpiComparison:
    match: CpiMatch
    nowcast: Nowcast
    sigma: float
    rows: list[BracketCompare]
    model_sigma_note: str


def effective_sigma(cfg: Config) -> float:
    """The uncertainty actually used, never tighter than the measured track record.

    Config is authoritative when it is at least as wide as what the source's own
    history supports. When it is tighter we widen, because a too-narrow sigma spikes
    centre-bracket probabilities and invents edge; a too-wide one only flattens toward
    50/50 and costs us signal. Only one of those two failure modes can lose money.
    """
    return max(float(cfg.macro_cpi_nowcast_sigma), _MEASURED_NOWCAST_RMSE_PP)


def _implied_sigma(rows: list[BracketCompare]) -> float | None:
    """Rough sigma implied by a set of market prices, for the variance-disagreement check.

    Second moment of the market's own distribution over the interior (finite) bins.
    Tails are skipped - they have no finite centre - so this is only meaningful when
    the interior carries most of the mass, which is the case whenever it matters.
    """
    finite = [r for r in rows if r.bracket.lo != -math.inf and r.bracket.hi != math.inf]
    mass = sum(r.bracket.market_prob for r in finite)
    if mass <= 0.5:
        return None
    mids = [((r.bracket.lo + r.bracket.hi) / 2.0, r.bracket.market_prob / mass) for r in finite]
    mean = sum(x * p for x, p in mids)
    var = sum(p * (x - mean) ** 2 for x, p in mids)
    return math.sqrt(var) if var > 0 else None


def compare_cpi(match: CpiMatch, nowcast: Nowcast, cfg: Config) -> CpiComparison:
    """Model vs market, bracket by bracket, with the honest caveats attached."""
    sigma = effective_sigma(cfg)
    rows: list[BracketCompare] = []
    for b in match.brackets:
        p = bracket_prob(b.lo, b.hi, nowcast.value_pp, sigma)
        gap = (p - b.market_prob) * 100.0
        rows.append(BracketCompare(bracket=b, model_prob=p, gap_c=gap,
                                   suspect=abs(gap) > cfg.macro_max_gap_c))
    note = ""
    mkt_sigma = _implied_sigma(rows)
    if mkt_sigma is not None and mkt_sigma > 0:
        ratio = sigma / mkt_sigma
        if ratio > 1.35 or ratio < 0.74:
            # Systematic, not directional. When our distribution is wider than the
            # market's, EVERY centre bracket reads "overpriced" and every tail reads
            # "cheap" - that is one disagreement about variance wearing the costume of
            # a dozen independent edges. Say so rather than let it print as signal.
            note = (f"VARIANCE DISAGREEMENT: model sigma {sigma:.3f}pp vs market-implied "
                    f"{mkt_sigma:.3f}pp (x{ratio:.2f}). The per-bracket gaps below are "
                    f"mostly this one effect, not {len(rows)} separate edges.")
    return CpiComparison(match, nowcast, sigma, rows, note)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _get(url: str, params):
    import requests
    r = requests.get(url, params=params, timeout=45,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def _fmt_bounds(b: Bracket) -> str:
    lo = "-inf" if b.lo == -math.inf else f"{b.lo:.2f}"
    hi = "+inf" if b.hi == math.inf else f"{b.hi:.2f}"
    return f"[{lo},{hi})"


def run_cpi(cfg: Config, *, fetch=None) -> list[CpiComparison]:
    """Every live CPI bracket set we can honestly price against the nowcast."""
    events = fetch_cpi_events(fetch=fetch)
    matches = match_cpi_events(events)
    cache: dict[tuple[str, str], dict[str, Nowcast]] = {}
    out: list[CpiComparison] = []
    for mt in matches:
        key = (mt.series, mt.basis)
        if key not in cache:
            cache[key] = fetch_nowcasts(mt.series, mt.basis, fetch=fetch)
        nc = nowcast_for(cache[key], mt.ref_month)
        if nc is None:
            continue                     # no nowcast, or already printed: fail closed
        out.append(compare_cpi(mt, nc, cfg))
    return out


def main(argv: list[str] | None = None) -> int:
    """python -m predictionedge.macrofv [--ff-price P --ff-month YYYY-MM --meeting-day D]

    Prints live Polymarket CPI brackets beside the Cleveland Fed nowcast-implied
    probability and the gap. The Fed leg reports its missing feed unless you supply a
    fed funds futures price yourself.
    """
    ap = argparse.ArgumentParser(prog="predictionedge.macrofv")
    ap.add_argument("--ff-price", type=float,
                    help="30-day fed funds futures price you sourced yourself "
                         "(no free feed exists - see the module docstring)")
    ap.add_argument("--ff-month", help="contract month YYYY-MM, for --ff-price")
    ap.add_argument("--meeting-day", type=int,
                    help="day of that month the FOMC decision lands, for --ff-price")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    if not cfg.macro_enabled:
        print("macro fair value disabled (cfg.macro_enabled=False)")
        return 0

    print("=" * 78)
    print("FED LEG")
    print("=" * 78)
    _fed_leg(args)

    print()
    print("=" * 78)
    print("CPI LEG  (Cleveland Fed nowcast, free, no key)")
    print("=" * 78)
    comparisons = run_cpi(cfg)
    if not comparisons:
        print("no live CPI bracket set could be matched and priced (fail-closed)")
        return 0

    sigma = effective_sigma(cfg)
    if sigma > cfg.macro_cpi_nowcast_sigma + 1e-9:
        print(f"NOTE: widened sigma to the measured floor {sigma:.2f}pp "
              f"(cfg.macro_cpi_nowcast_sigma={cfg.macro_cpi_nowcast_sigma:.2f}pp is tighter "
              f"than the Cleveland Fed's own 154-vintage track record supports).")
    print()

    for cmp_ in comparisons:
        m = cmp_.match
        print(f"-- {m.title}")
        print(f"   {m.series} {m.basis}  ref={m.ref_month}  nowcast={cmp_.nowcast.value_pp:+.3f}pp "
              f"sigma={cmp_.sigma:.2f}pp  as_of={cmp_.nowcast.as_of}")
        print(f"   https://polymarket.com/event/{m.slug}")
        if cmp_.model_sigma_note:
            print(f"   !! {cmp_.model_sigma_note}")
        print(f"   {'bracket':>10} {'wins on':>16} {'market':>8} {'model':>8} {'gap(c)':>8}")
        for r in sorted(cmp_.rows, key=lambda x: x.bracket.lo):
            flag = "  <-- SUSPECT, likely a bad match not an edge" if r.suspect else ""
            print(f"   {r.bracket.label:>10} {_fmt_bounds(r.bracket):>16} "
                  f"{r.bracket.market_prob:8.3f} {r.model_prob:8.3f} {r.gap_c:+8.1f}{flag}")
        print()

    print("Advisory only. Verify the resolution text before acting: bracket edges assume")
    print("BLS one-decimal rounding, and the seasonal-adjustment basis must match.")
    return 0


def _fed_leg(args) -> None:
    """Report the Fed reference, or explain precisely why there isn't one."""
    tr = fetch_target_range()
    if tr is None:
        print("current FOMC target range unavailable (NY Fed unreachable) - fail closed")
        return
    lo, hi = tr
    print(f"current FOMC target range: {lo:.2f}% - {hi:.2f}%  (NY Fed, free, no key)")

    if args.ff_price is None:
        print()
        print("NO FED REFERENCE PRICE. Fed funds futures are not obtainable free:")
        print("  * CME quote API -> HTTP 403, explicitly prohibits scripted access (hard stop)")
        print("  * FRED          -> unreachable here, and carries realised rates, not futures")
        print("  * Atlanta Fed MPT -> free and reachable, but prices average SOFR over a")
        print("                       3-month window, which is not a per-meeting claim")
        print("Reporting this rather than substituting an approximation, per EDGE_PLAN.md.")
        print("Supply a price yourself with --ff-price/--ff-month/--meeting-day to use the maths.")
        return

    if not args.ff_month or args.meeting_day is None:
        print("--ff-price needs --ff-month YYYY-MM and --meeting-day")
        return
    try:
        year, month = (int(x) for x in args.ff_month.split("-"))
        days = _days_in_month(year, month)
        dec = decompose_meeting(args.ff_price, rate_before=(lo + hi) / 2.0,
                                meeting_day=args.meeting_day, days_in_month=days)
    except ValueError as exc:
        print(f"cannot decompose: {exc}")
        return

    print(f"implied average EFFR for {args.ff_month}: {dec.implied_average:.4f}%")
    print(f"implied post-meeting rate:              {dec.rate_after:.4f}%")
    probs = one_meeting_outcome_probs(dec.rate_after, dec.rate_before)
    if probs is None:
        print("implied move exceeds one 25bp step: a two-point decomposition cannot")
        print("represent the 50+bp tails, so no probabilities are reported (fail closed).")
        return
    for name, p in probs.items():
        print(f"   {name:>18}: {p:6.3f}")
    print("Compare against the 'Fed Decision in <month>?' negRisk set on Polymarket -")
    print("note its legs are EXACTLY 25bp and open-ended 50+, not 'X or more'.")


def _days_in_month(year: int, month: int) -> int:
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    return (nxt - date(year, month, 1)).days


if __name__ == "__main__":
    raise SystemExit(main())
