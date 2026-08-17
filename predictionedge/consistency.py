"""Logical-consistency violations *within* Polymarket - edges that need no forecast.

A price can be wrong about the world and still be a fine price. These are different:
they are prices that contradict *each other*, so at least one is wrong as a matter of
arithmetic. P(BTC above $150k) cannot exceed P(BTC above $120k). A set of mutually
exclusive outcomes cannot all be buyable for less than $1.00 in total. Being right about
the future is not a prerequisite for collecting.

WHY THIS IS NOT `crossvenue.py` AGAIN. That module read "will X run for president" and
"will X win the presidency" as one question and reported a 48c edge that never existed,
because the only thing tying the two titles together was word overlap. Here **the venue
asserts the relationship**: the legs live inside one Gamma event, they carry a
``negRisk`` flag that states mutual exclusivity, and their ``groupItemTitle`` is the
structured threshold. Nothing below matches on title similarity. Where structure runs
out - the dominance family, which spans two events - the relationship comes from a
hand-audited registry naming the exact legs, never from a fuzzy match.

Verified against live Gamma on 2026-08-06:
  - ``negRisk`` IS exposed, on both the event and each nested market. It is real and
    load-bearing, not an undocumented hope.
  - ``negRisk: true`` marks an *exclusive partition* of exact buckets, NOT a ladder.
    `what-will-the-fed-rate-be-at-the-end-of-2026` is negRisk with legs "2.25%", "2.5%",
    ... "4.0%" priced 0.0045 .. 0.376. Read as a ">=" ladder that is a 27c inversion on
    every pair and pure fiction. Hence: negRisk events go to the bracket family only,
    and never to the ladder family.
  - ``negRisk: false`` grouped events carry the genuine nested ladders
    (`opensea-fdv-above-one-day-after-launch`: "above $100M" .. "above $5B").
  - Settled legs stay in a live event's payload with ``closed: true``, ``bestBid: null``
    and prices ["0","1"] (`netanyahu-out-before-2027`). Reading those as live quotes
    manufactures a violation on every pair. Missing/None prices REJECT.

Everything is priced on the side you would actually have to hit - buy at the ask, sell
at the bid - so a reported gap is already net of the round-trip spread. A violation
narrower than the spread arrives as a negative number and is dropped.

ADVISORY ONLY. This module reads public data and prints. There is no order path here
and there must never be one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"

# Gamma caps a page at 100 regardless of what `limit` asks for, so crawl with offset.
_PAGE = 100


def _f(v) -> float | None:
    """Float, or None. Deliberately not 0.0-on-failure: a missing price must reject."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --- the objects ------------------------------------------------------------

@dataclass(frozen=True)
class Leg:
    """One tradeable side of a violation, priced on both books.

    ``bid``/``ask`` are the *executable* prices. Every number downstream is computed
    from these, never from the mid or the last trade, because a violation you can only
    collect at the mid is not a violation.
    """

    condition_id: str
    event_slug: str
    question: str
    name: str            # groupItemTitle - the leg's label within its event
    bid: float
    ask: float
    liquidity: float
    end_iso: str

    @property
    def url(self) -> str:
        return f"https://polymarket.com/event/{self.event_slug}" if self.event_slug else ""

    @property
    def short(self) -> str:
        return self.question[:70] if self.question else self.name


@dataclass(frozen=True)
class Violation:
    """A constraint that the live book breaks, with the trade that collects on it."""

    family: str                  # "ladder" | "bracket" | "dominance"
    event_slug: str
    constraint: str              # the rule, in words, that the prices contradict
    edge_c: float                # net-of-spread cents, positive by construction
    legs: tuple[Leg, ...]
    actions: tuple[str, ...]     # what to buy, at what price
    warnings: tuple[str, ...] = ()


@dataclass
class ScanReport:
    """Violations plus an honest account of every group that was thrown away."""

    violations: list[Violation] = field(default_factory=list)
    events_scanned: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


# --- parsing legs out of a Gamma event --------------------------------------

def parse_leg(market: dict, event_slug: str) -> Leg | None:
    """A live, two-sided, quoted leg - or None, which always means *reject*.

    Fail closed on every axis. A settled leg still ships inside a live event's payload
    with ``closed: true`` and a null bid; treating that as a real quote invents a
    violation against every other leg in the group. So does a one-sided book: if you
    cannot both buy and sell, you cannot close the trade the violation implies.
    """
    if not isinstance(market, dict):
        return None
    if market.get("closed") or not market.get("active", True):
        return None
    if market.get("acceptingOrders") is False:
        return None

    bid, ask = _f(market.get("bestBid")), _f(market.get("bestAsk"))
    if bid is None or ask is None:
        return None
    # A leg with no bid cannot be sold and a leg with no ask cannot be bought; either
    # way one side of the arbitrage is imaginary. Crossed books are bad data, not edge.
    if not (0.0 < bid <= 1.0) or not (0.0 < ask <= 1.0) or ask < bid:
        return None

    cid = market.get("conditionId") or market.get("condition_id") or ""
    if not cid:
        return None

    liq = _f(market.get("liquidity"))
    if liq is None:
        liq = _f(market.get("liquidityNum"))
    if liq is None:
        return None      # unknown depth is not "enough depth"

    return Leg(
        condition_id=cid,
        event_slug=event_slug,
        question=str(market.get("question") or ""),
        name=str(market.get("groupItemTitle") or "").strip(),
        bid=bid,
        ask=ask,
        liquidity=liq,
        end_iso=str(market.get("endDate") or market.get("endDateIso") or ""),
    )


# --- threshold parsing ------------------------------------------------------

_MULT = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}

# "$500M", "1.5B", "40+", "3.75%", "150,000", ">= 4.5%". The comparator and the trailing
# "+" are captured because a leg that carries its own comparator is telling us the group
# is not a clean one-sided ladder unless every leg agrees.
_NUM = re.compile(
    r"^\s*(?P<cmp>>=|<=|>|<|≥|≤)?\s*\$?\s*"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<mult>[kKmMbBtT])?\s*%?\s*(?P<plus>\+)?\s*$"
)


def numeric_threshold(text: str) -> float | None:
    """The number a group label names, scaled for k/M/B/T. None if it isn't one."""
    m = _NUM.match(text or "")
    if not m:
        return None
    try:
        value = float(m.group("num").replace(",", ""))
    except ValueError:
        return None
    mult = (m.group("mult") or "").lower()
    return value * _MULT.get(mult, 1.0)


def _label_comparator(text: str) -> str | None:
    """"+" / ">=" / "<=" carried by the label itself, if any."""
    m = _NUM.match(text or "")
    if not m:
        return None
    if m.group("plus"):
        return ">="
    cmp_ = m.group("cmp")
    if cmp_ in (">=", "≥"):
        return ">="
    if cmp_ in ("<=", "≤"):
        return "<="
    return cmp_ or None


# Wording that makes a group one-sided. Without one of these the group may be a set of
# EXACT buckets ("be 3.75% at the end of 2026"), where monotonicity does not hold at all
# and every pair looks inverted. No marker => reject, never assume.
_UP = re.compile(
    r"\b(above|over|more than|greater than|greater|at least|exceeds?|exceeding|"
    r"reach(es|ed)?|hits?|or more|or higher|higher than|surpass(es)?)\b", re.IGNORECASE)
_DOWN = re.compile(
    r"\b(below|under|less than|fewer than|at most|or less|or lower|or fewer|"
    r"lower than|dips? below|falls? below)\b", re.IGNORECASE)
# "by <date>" / "before <date>" is the cumulative-deadline shape: a later deadline is a
# strict superset of an earlier one, so probability is non-decreasing in the date.
#
# The deadline is read out of the QUESTION and nowhere else. The obvious key - each
# market's ``endDate`` - is wrong often enough to invert the ladder, which is the worst
# possible failure here because an inverted ladder reports every correctly-priced rung
# as a violation. Live counter-examples, all on 2026-08-06:
#   `will-russia-capture-lyman-in-2025` prices "by December 31, 2026" on an endDate of
#     2025-12-31 - a year adrift, and behind the "September 30, 2026" rung.
#   `gpt-6-released-by` prices "by September 30, 2026" on an endDate of 2026-06-30.
#   `will-samuel-alito-announce-his-retirement-by` gives EVERY rung the event's own
#     2026-12-31 end date, so endDate carries no deadline information at all.
# Trusting endDate produced six confident, fully-backwards violations before this.
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), start=1)}

_DEADLINE_PATTERNS = (
    # "by August 7, 2026" - the only unambiguous form, so it is tried first.
    re.compile(r"\b(?:by|before|prior to|on or before)\s+(?P<mon>[A-Za-z]{3,9})\s+"
               r"(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})\b", re.IGNORECASE),
    # "by the end of 2026"
    re.compile(r"\b(?:by|before|prior to|on or before)\s+(?:the\s+)?end\s+of\s+"
               r"(?P<year>\d{4})\b", re.IGNORECASE),
    # "by June 2026" - taken as the last instant of that month.
    re.compile(r"\b(?:by|before|prior to|on or before)\s+(?P<mon>[A-Za-z]{3,9})\s+"
               r"(?P<year>\d{4})\b", re.IGNORECASE),
)


def deadline_from_question(text: str) -> datetime | None:
    """The dated deadline a question names, or None - which always means *reject*.

    A four-digit year is mandatory. "Netanyahu out by May 31?" appears twice in one live
    event, once meaning 2025 and once 2026; ordering those by month alone silently
    reverses two rungs, and a reversed rung is a manufactured edge.
    """
    import calendar

    for pattern in _DEADLINE_PATTERNS:
        m = pattern.search(text or "")
        if not m:
            continue
        year = int(m.group("year"))
        groups = m.groupdict()
        mon_text = (groups.get("mon") or "").lower()
        if mon_text:
            month = _MONTHS.get(mon_text)
            if month is None:
                return None      # "by the 2026 deadline" is not a date. Reject.
        else:
            month = 12           # "by the end of <year>"
        day = int(groups["day"]) if groups.get("day") else calendar.monthrange(year, month)[1]
        try:
            return datetime(year, month, day, 23, 59, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _direction(legs: tuple[Leg, ...]) -> int | None:
    """+1 if a bigger threshold is a HARDER claim, -1 if easier, None if unreadable.

    Every leg must agree. A group where some legs say "above" and others "below" is not
    one ladder, and guessing which way it runs is exactly how a fake edge gets made.
    """
    ups = downs = 0
    for leg in legs:
        text = f"{leg.question} {leg.name}"
        cmp_ = _label_comparator(leg.name)
        up = bool(_UP.search(text)) or cmp_ == ">="
        down = bool(_DOWN.search(text)) or cmp_ == "<="
        if up and down:
            return None          # "above X or below Y" - not a ladder rung
        ups += up
        downs += down
    if ups and not downs:
        return 1                 # P(above X) falls as X rises
    if downs and not ups:
        return -1                # P(below X) rises as X rises
    return None                  # silent, or self-contradictory: reject


# --- family 1: ladder monotonicity ------------------------------------------

def _pair_edge_c(cheap: Leg, dear: Leg) -> float:
    """Cents earned by the pair trade that the constraint P(cheap) >= P(dear) implies.

    Long YES on the leg that must be worth *more*, long NO on the leg that must be worth
    *less*. That basket pays 1 + [P(cheap) - P(dear)] >= 1.00 in every state of the
    world, and costs ask(cheap) + (1 - bid(dear)). So it is free money exactly when
    ask(cheap) < bid(dear), and the gap is already net of both spreads because both
    numbers are the side you have to hit.
    """
    return round((dear.bid - cheap.ask) * 100.0, 2)


def ladder_violations(legs: list[Leg], *, min_edge_c: float,
                      min_liquidity: float, report: ScanReport | None = None,
                      event_slug: str = "") -> list[Violation]:
    """Inversions in one already-validated ladder, ordered easiest claim first.

    ``legs`` must already be a genuine one-sided ladder; ordering and eligibility are
    decided by :func:`scan_events`, which is where the negRisk and wording gates live.
    """
    out: list[Violation] = []
    for i, easier in enumerate(legs):
        for harder in legs[i + 1:]:
            if min(easier.liquidity, harder.liquidity) < min_liquidity:
                if report:
                    report.reject("ladder pair too illiquid to fill")
                continue
            edge = _pair_edge_c(easier, harder)
            if edge < min_edge_c:
                if report and edge > 0:
                    report.reject("ladder inversion smaller than the spread")
                continue
            out.append(Violation(
                family="ladder",
                event_slug=event_slug or easier.event_slug,
                constraint=(f"P({harder.name}) must be <= P({easier.name}), "
                            f"but {harder.name} bids {harder.bid:.3f} while "
                            f"{easier.name} offers {easier.ask:.3f}"),
                edge_c=edge,
                legs=(easier, harder),
                actions=(f"BUY YES  {easier.name}  @ {easier.ask:.3f}",
                         f"BUY NO   {harder.name}  @ {1.0 - harder.bid:.3f}"),
                warnings=("both legs settle on the same event page - confirm the two "
                          "rule texts differ only in the threshold",),
            ))
    return out


# --- family 2: bracket sums -------------------------------------------------

def bracket_violation(legs: list[Leg], *, min_edge_c: float, event_slug: str,
                      report: ScanReport | None = None) -> Violation | None:
    """Sum(asks) < 1.00 or Sum(bids) > 1.00 across one exclusive, exhaustive set.

    ``negRisk`` is Polymarket asserting that exactly one of these legs resolves YES, so
    the true prices sum to 1.00. Buying every leg at its ask therefore costs Sum(asks)
    and pays exactly 1.00; selling every leg costs Sum(1 - bid) = n - Sum(bids) and pays
    n - 1. Both directions are already net of spread - they use the side you must hit.

    Requires EVERY leg. A partial set is not a partition, and a subset that offers under
    1.00 is arithmetic, not edge, so the caller must hand this a complete group.
    """
    if len(legs) < 2:
        return None
    sum_ask = sum(l.ask for l in legs)
    sum_bid = sum(l.bid for l in legs)

    # Both cannot hold at once (it would need Sum(bid) > 1 > Sum(ask) with bid <= ask on
    # every leg). If the book says otherwise the data is broken, not generous.
    if sum_ask < 1.0 and sum_bid > 1.0:
        if report:
            report.reject("bracket book inconsistent (bid sum above ask sum)")
        return None

    n = len(legs)
    warn = (f"requires filling all {n} legs - confirm the event page lists exactly {n} "
            f"outcomes before trusting the sum",)

    if sum_ask < 1.0:
        edge = round((1.0 - sum_ask) * 100.0, 2)
        if edge < min_edge_c:
            if report:
                report.reject("bracket underround smaller than the spread")
            return None
        return Violation(
            family="bracket", event_slug=event_slug,
            constraint=(f"{n} mutually exclusive outcomes offer at {sum_ask:.4f} "
                        f"total, but exactly one of them pays 1.00"),
            edge_c=edge, legs=tuple(legs),
            actions=tuple(f"BUY YES  {l.name}  @ {l.ask:.3f}" for l in legs),
            warnings=warn,
        )

    if sum_bid > 1.0:
        edge = round((sum_bid - 1.0) * 100.0, 2)
        if edge < min_edge_c:
            if report:
                report.reject("bracket overround smaller than the spread")
            return None
        return Violation(
            family="bracket", event_slug=event_slug,
            constraint=(f"{n} mutually exclusive outcomes bid {sum_bid:.4f} total, "
                        f"but exactly one of them pays 1.00"),
            edge_c=edge, legs=tuple(legs),
            actions=tuple(f"BUY NO   {l.name}  @ {1.0 - l.bid:.3f}" for l in legs),
            warnings=warn,
        )
    return None


# --- family 3: dominance ----------------------------------------------------

@dataclass(frozen=True)
class DominanceRule:
    """One hand-audited implication between two Gamma events, leg by leg.

    The event pair AND the legs it covers are both audited, because the leg list is
    where an automatic version dies. Live, `presidential-election-winner-2028` shares 92
    ``groupItemTitle`` values with `democratic-presidential-nominee-2028`, and 70 of
    those also appear in the *Republican* nominee event - alongside filler legs named
    "Person AA" through "Person BL". Intersecting names automatically would assert
    P(Vance wins the election) <= P(Vance wins the DEMOCRATIC nomination) and print a
    20c edge that is pure nonsense. Nothing in the Gamma payload states a leg's party,
    so this relation cannot be derived from structure. It is enumerated or not shipped.
    """

    harder_event: str          # event slug whose legs are the STRICTER claim
    easier_event: str          # event slug whose legs are the prerequisite
    legs: tuple[str, ...]      # exact group titles this rule was audited for
    note: str
    caveat: str


# Audited 2026-08-06 against live Gamma. A name absent from either event simply never
# pairs up, so listing a spare name is harmless; listing a WRONG one is not.
DOMINANCE_RULES: tuple[DominanceRule, ...] = (
    DominanceRule(
        harder_event="presidential-election-winner-2028",
        easier_event="republican-presidential-nominee-2028",
        legs=("Donald Trump", "J.D. Vance", "JD Vance", "Marco Rubio", "Ron DeSantis",
              "Nikki Haley", "Glenn Youngkin", "Tucker Carlson", "Vivek Ramaswamy",
              "Tulsi Gabbard", "Josh Hawley", "Greg Abbott", "Ted Cruz"),
        note="winning the presidency requires being on the ballot as the GOP nominee",
        caveat="assumes no independent/third-party run - the one way this constraint breaks",
    ),
    DominanceRule(
        harder_event="presidential-election-winner-2028",
        easier_event="democratic-presidential-nominee-2028",
        legs=("Gavin Newsom", "Alexandria Ocasio-Cortez", "Pete Buttigieg",
              "Josh Shapiro", "Gretchen Whitmer", "Kamala Harris", "Andy Beshear",
              "Wes Moore", "Cory Booker", "Ro Khanna", "Jasmine Crockett",
              "Raphael Warnock", "Amy Klobuchar"),
        note="winning the presidency requires being on the ballot as the Dem nominee",
        caveat="assumes no independent/third-party run - the one way this constraint breaks",
    ),
)


def _norm_name(s: str) -> str:
    """Leg identity, punctuation-insensitive: 'J.D. Vance' and 'JD Vance' are one man."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def dominance_violations(rule: DominanceRule, harder_legs: list[Leg],
                         easier_legs: list[Leg], *, min_edge_c: float,
                         min_liquidity: float,
                         report: ScanReport | None = None) -> list[Violation]:
    """Legs where the strictly-harder claim outbids its own prerequisite.

    ``question_conflict`` is used here in the direction that actually helps. This family
    is *built on* an asymmetry - "win the election" vs "win the nomination" is supposed
    to be a different question - so a conflict is the expected, healthy state. What the
    guard protects against is the registry going stale: if Polymarket renames an event
    and the two questions stop differing in any decisive term, the strict inequality
    this rule asserts is no longer justified. Hence the rejection is on *not* conflict.
    """
    from .crossvenue import question_conflict

    allowed = {_norm_name(n) for n in rule.legs}
    easier_by_name = {_norm_name(l.name): l for l in easier_legs}
    out: list[Violation] = []
    for harder in harder_legs:
        key = _norm_name(harder.name)
        if key not in allowed:
            continue                    # not audited => not asserted => not checked
        easier = easier_by_name.get(key)
        if easier is None:
            if report:
                report.reject("dominance leg missing on the prerequisite event")
            continue
        if not question_conflict(harder.question, easier.question):
            if report:
                report.reject("dominance rule stale (questions no longer differ)")
            continue
        if min(harder.liquidity, easier.liquidity) < min_liquidity:
            if report:
                report.reject("dominance pair too illiquid to fill")
            continue
        edge = _pair_edge_c(easier, harder)
        if edge < min_edge_c:
            if report and edge > 0:
                report.reject("dominance gap smaller than the spread")
            continue
        out.append(Violation(
            family="dominance", event_slug=harder.event_slug,
            constraint=(f"P({harder.name} / {rule.harder_event}) must be <= "
                        f"P({harder.name} / {rule.easier_event}) - {rule.note}"),
            edge_c=edge, legs=(easier, harder),
            actions=(f"BUY YES  {easier.name} / {rule.easier_event}  @ {easier.ask:.3f}",
                     f"BUY NO   {harder.name} / {rule.harder_event}  "
                     f"@ {1.0 - harder.bid:.3f}"),
            warnings=(rule.caveat,
                      "two separate events - read both rule texts before trading"),
        ))
    return out


# --- classifying a Gamma event ----------------------------------------------

def event_is_negrisk(event: dict) -> bool:
    """True when Polymarket asserts this event's legs are mutually exclusive.

    Checked on the event *and* on every nested market, because the flag has appeared on
    the markets while the event carried ``None``. Any assertion of exclusivity is
    binding: it routes the group to the bracket family and, critically, *away* from the
    ladder family, where exclusive buckets would read as an inversion on every pair.
    """
    if event.get("negRisk") or event.get("enableNegRisk") or event.get("negRiskAugmented"):
        return True
    return any(m.get("negRisk") for m in (event.get("markets") or [])
               if isinstance(m, dict))


def ladder_rungs(event: dict, legs: list[Leg]
                 ) -> tuple[list[Leg] | None, str, str]:
    """Order a group's live legs easiest-claim-first, or say why it is not a ladder.

    Returns ``(ordered_legs, kind, "")`` on success and ``(None, "", reason)`` on
    rejection. Structure is judged from EVERY open market in the event, not just the
    ones we could price: if a rung's label is unreadable we do not understand the group,
    and a group we do not understand cannot be declared monotone.
    """
    markets = [m for m in (event.get("markets") or []) if isinstance(m, dict)]
    open_markets = [m for m in markets if not m.get("closed")]
    if len(legs) < 2:
        return None, "", "fewer than two live, quoted legs"

    labels = [str(m.get("groupItemTitle") or "").strip() for m in open_markets]
    if not labels or any(not s for s in labels):
        return None, "", "a live leg carries no group label"

    if all(numeric_threshold(s) is not None for s in labels):
        # A threshold ladder is one quantity measured at one moment. Rungs with
        # different end dates are different questions that happen to share a page.
        ends = {str(m.get("endDate") or "") for m in open_markets}
        if len(ends) != 1 or "" in ends:
            return None, "", "numeric group spans different end dates"
        direction = _direction(tuple(legs))
        if direction is None:
            return None, "", "no one-sided wording (may be exact buckets)"
        keyed = [(numeric_threshold(l.name), l) for l in legs]
        keyed = _drop_ambiguous(keyed)
        if len(keyed) < 2:
            return None, "", "duplicate thresholds left too few rungs"
        # direction +1: P(above X) falls as X rises, so the smallest X is the easiest
        # claim. direction -1 ("below X") runs the other way.
        keyed.sort(key=lambda kv: kv[0], reverse=(direction < 0))
        return [l for _, l in keyed], "numeric", ""

    # Deadline ladder: "out by March 31, 2026" is contained in "out by December 31,
    # 2026". Every live rung must name a dated deadline in its own question - see
    # `deadline_from_question` for why endDate is not usable and what it cost.
    question_dates = [deadline_from_question(str(m.get("question") or ""))
                      for m in open_markets]
    if all(d is not None for d in question_dates):
        keyed = []
        for leg in legs:
            when = deadline_from_question(leg.question)
            if when is None:
                return None, "", "deadline group has an undated question"
            keyed.append((when.timestamp(), leg))
        keyed = _drop_ambiguous(keyed)
        if len(keyed) < 2:
            return None, "", "duplicate deadlines left too few rungs"
        # A later deadline is a superset of an earlier one, so it is the EASIER claim
        # and must sort first: ladder_violations reads legs[i] as easier than legs[j>i].
        keyed.sort(key=lambda kv: kv[0], reverse=True)
        return [l for _, l in keyed], "deadline", ""

    return None, "", "group labels are not thresholds or deadlines"


def _drop_ambiguous(keyed: list[tuple[float, Leg]]) -> list[tuple[float, Leg]]:
    """Remove every leg whose rung value is shared with another leg.

    Two live markets on the same rung are either a duplicate listing or differ in a way
    the payload does not show (`opensea-fdv-above-one-day-after-launch` lists "$100M"
    twice). Either way the pair asserts equality rather than an ordering, so comparing
    them is not a monotonicity test. Drop them and keep the rungs we do understand.
    """
    seen: dict[float, int] = {}
    for value, _ in keyed:
        seen[value] = seen.get(value, 0) + 1
    return [(v, l) for v, l in keyed if seen[v] == 1]


# --- the scan ---------------------------------------------------------------

def scan_events(events: list[dict], *, min_edge_c: float, min_liquidity: float,
                max_bracket_legs: int = 12,
                report: ScanReport | None = None) -> ScanReport:
    """Run every family over an already-fetched list of Gamma events."""
    rep = report or ScanReport()
    by_slug: dict[str, list[Leg]] = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("closed") or not event.get("active", True):
            continue
        slug = str(event.get("slug") or "")
        markets = [m for m in (event.get("markets") or []) if isinstance(m, dict)]
        if len(markets) < 2:
            continue                    # a single binary market cannot contradict itself
        rep.events_scanned += 1

        legs = [leg for leg in (parse_leg(m, slug) for m in markets) if leg is not None]
        by_slug[slug] = legs

        if event_is_negrisk(event):
            open_count = sum(1 for m in markets if not m.get("closed"))
            # Every leg or nothing. Buying "the whole set" minus a leg you could not
            # price is not a hedge, it is a directional bet with an arbitrage's sizing.
            if len(legs) < open_count or len(legs) != len(markets):
                rep.reject("bracket has legs we cannot price (fail closed)")
                continue
            if len(legs) > max_bracket_legs:
                rep.reject("bracket has too many legs to place by hand")
                continue
            if min(l.liquidity for l in legs) < min_liquidity:
                rep.reject("bracket leg too illiquid to fill")
                continue
            hit = bracket_violation(legs, min_edge_c=min_edge_c, event_slug=slug,
                                    report=rep)
            if hit is not None:
                rep.violations.append(hit)
            continue

        rungs, _kind, why = ladder_rungs(event, legs)
        if rungs is None:
            rep.reject(f"not a ladder: {why}")
            continue
        rep.violations += ladder_violations(
            rungs, min_edge_c=min_edge_c, min_liquidity=min_liquidity,
            report=rep, event_slug=slug)

    for rule in DOMINANCE_RULES:
        harder, easier = by_slug.get(rule.harder_event), by_slug.get(rule.easier_event)
        if not harder or not easier:
            rep.reject("dominance rule: one of its events was not in the crawl")
            continue
        rep.violations += dominance_violations(
            rule, harder, easier, min_edge_c=min_edge_c,
            min_liquidity=min_liquidity, report=rep)

    rep.violations.sort(key=lambda v: v.edge_c, reverse=True)
    return rep


# --- fetching ---------------------------------------------------------------

def _requests_get(url: str, params):
    import requests
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _rows(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("data") or payload.get("events") or []
    return []


def fetch_events(*, max_events: int = 400, base: str = GAMMA, fetch=None) -> list[dict]:
    """Active, open Gamma events, biggest first. Offset pagination - Gamma caps at 100."""
    getter = fetch or _requests_get
    out: list[dict] = []
    for offset in range(0, max(_PAGE, max_events), _PAGE):
        try:
            batch = _rows(getter(f"{base}/events", {
                "active": "true", "closed": "false", "limit": _PAGE,
                "offset": offset, "order": "volume", "ascending": "false"}))
        except Exception:  # noqa: BLE001
            break
        if not batch:
            break
        out += batch
        if len(batch) < _PAGE:
            break
    return out[:max_events]


def fetch_events_by_slug(slugs, *, base: str = GAMMA, fetch=None) -> list[dict]:
    """Fetch named events one slug at a time.

    One request per slug rather than repeated params: Gamma rejects large batches of
    repeated values outright (the reason ``gamma.market_meta`` chunks at 20), and the
    dominance registry is a handful of slugs, so there is nothing to save by batching.
    """
    getter = fetch or _requests_get
    out: list[dict] = []
    for slug in slugs:
        try:
            out += _rows(getter(f"{base}/events", {"slug": slug}))
        except Exception:  # noqa: BLE001
            continue        # a missing registry event rejects later, it does not pass
    return out


def scan(cfg, *, base: str = GAMMA, fetch=None,
         max_bracket_legs: int = 12) -> ScanReport:
    """Crawl live Polymarket and return every consistency violation worth a human."""
    rep = ScanReport()
    if not getattr(cfg, "consistency_enabled", True):
        rep.notes.append("consistency scanner disabled (PE_CONSISTENCY_ENABLED=0)")
        return rep

    events = fetch_events(max_events=cfg.consistency_max_events, base=base, fetch=fetch)
    if not events:
        # The market_meta lesson: an empty upstream must not read as "all clear".
        rep.notes.append("Gamma returned no events - scan inconclusive, not clean")
        return rep

    have = {str(e.get("slug") or "") for e in events if isinstance(e, dict)}
    wanted = {s for r in DOMINANCE_RULES for s in (r.harder_event, r.easier_event)}
    missing = sorted(wanted - have)
    if missing:
        events += fetch_events_by_slug(missing, base=base, fetch=fetch)

    return scan_events(events, min_edge_c=cfg.consistency_min_edge_c,
                       min_liquidity=cfg.consistency_min_liquidity,
                       max_bracket_legs=max_bracket_legs, report=rep)


# --- CLI --------------------------------------------------------------------

def _ascii(s: str) -> str:
    """Console-safe text.

    Leg names come straight from Gamma and are full Unicode - the live French election
    event alone carries "Eric Zemmour" and "Edouard Philippe" with accents. A Windows
    console defaults to cp1252 and raises UnicodeEncodeError mid-print, which would kill
    the scan after it had already found the answer.
    """
    return (s or "").encode("ascii", "replace").decode("ascii")


def _print_violation(v: Violation) -> None:
    print(f"\n  [{v.family}] +{v.edge_c:.2f}c net of spread   {_ascii(v.event_slug)}")
    print(f"    constraint: {_ascii(v.constraint)}")
    for leg in v.legs:
        print(f"    leg: {_ascii(leg.name) or '(unnamed)'} | bid {leg.bid:.3f} "
              f"ask {leg.ask:.3f} | liq ${leg.liquidity:,.0f} | {_ascii(leg.short)}")
    # Dominance spans two events, so print each distinct page; the others collapse to one.
    for url in dict.fromkeys(leg.url for leg in v.legs if leg.url):
        print(f"    {url}")
    for act in v.actions:
        print(f"    -> {_ascii(act)}")
    for warn in v.warnings:
        print(f"    ! {_ascii(warn)}")


def main(argv: list[str] | None = None) -> int:
    """python -m predictionedge.consistency [--show-rejected]

    Prints live logical-consistency violations on global Polymarket. Expect zero most
    days: these are the easiest edges to find and therefore the first ones taken.
    A clean scan is a result, not a failure.
    """
    import argparse
    from .config import Config

    ap = argparse.ArgumentParser(prog="predictionedge.consistency")
    ap.add_argument("--min-edge-c", type=float, default=None,
                    help="cents floor, net of spread (default: cfg)")
    ap.add_argument("--max-events", type=int, default=None)
    ap.add_argument("--min-liquidity", type=float, default=None)
    ap.add_argument("--max-bracket-legs", type=int, default=12,
                    help="a bracket you cannot place by hand is not a ticket")
    ap.add_argument("--show-rejected", action="store_true",
                    help="show why each group was thrown out")
    args = ap.parse_args(argv)

    # `Config` is frozen, so a CLI override REPLACES it rather than assigning into it -
    # the assignments this used to do raised FrozenInstanceError the moment any of the
    # three flags was passed. Frozen is the right shape and stays: a scan must not be
    # able to edit the config out from under whatever else holds a reference to it.
    cfg = Config.from_env()
    overrides = {name: value for name, value in (
        ("consistency_min_edge_c", args.min_edge_c),
        ("consistency_max_events", args.max_events),
        ("consistency_min_liquidity", args.min_liquidity),
    ) if value is not None}
    if overrides:
        cfg = replace(cfg, **overrides)

    rep = scan(cfg, max_bracket_legs=args.max_bracket_legs)
    print(f"scanned {rep.events_scanned} multi-market Polymarket event(s) | "
          f"floor {cfg.consistency_min_edge_c:.1f}c | "
          f"min liquidity ${cfg.consistency_min_liquidity:,.0f}")

    if args.show_rejected:
        print("\n-- groups rejected (each one is a fake edge that did not get printed) --")
        for reason, n in sorted(rep.rejected.items(), key=lambda kv: -kv[1]):
            print(f"  {n:5d}  {reason}")

    for note in rep.notes:
        print(f"  note: {note}")

    print(f"\n{len(rep.violations)} live violation(s):")
    for v in rep.violations:
        _print_violation(v)
    if not rep.violations:
        print("  none. Both legs of every group priced consistently, net of spread.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
