"""Ranked trade tickets for a human to place manually.

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

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .copytrade import is_esports, scan_smart_flow
from .edge import copy_give_up_c
from .sizing import Account, Sizing, event_url, size_position

# A real Polymarket market key is 0x + 64 hex. Combo tickets fake one at 62, zero-padded.
_CONDITION_ID = re.compile(r"0x[0-9a-fA-F]{64}")


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
    # Hours until the EVENT decides, not until the settlement deadline. On sports the
    # two differ by days - Polymarket pads `endDate` out to a tournament-wide date, so
    # a same-night ball game published as "174.6h to resolve". Deadline still ships as
    # `end_iso` for anyone who needs it.
    hours_to_resolve: float | None
    n_wallets: int
    whale_usd: float
    minutes_ago: float
    liquidity: float
    url: str
    # Absolutes, so a published snapshot can recompute "resolves in" at view time
    # rather than freezing the countdown at the moment it was generated.
    end_iso: str = ""
    event_iso: str = ""   # kickoff, when the venue gives us one; "" otherwise
    slug: str = ""        # intl market slug - the key cross-venue matching runs on
    signal_ts: float = 0.0
    # WHO bought and for how much, as (address, usd) pairs, in PLAINTEXT. `n_wallets`
    # and `whale_usd` are summaries of exactly this, and the concentration and bankroll
    # rules are computed from it, so the board was publishing conclusions whose inputs
    # it threw away. Addresses are not hashed because they are already public on Polygon
    # and on Polymarket's own leaderboard - a hash would protect nobody and would break
    # the join back to that leaderboard, which is the only reason to keep them at all:
    # "did the wallets we copied stay profitable" needs the address to be answerable.
    wallet_usd: tuple[tuple[str, float], ...] = ()
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
    # Cleared every hard filter but sits below the buy bar. Recorded, never recommended.
    # This is the only way the bar can ever be validated: if we observe outcomes only
    # above the threshold, the rejected region stays invisible forever and "0.45 is the
    # right cutoff" can never be checked against anything. The trial takes these, tagged
    # `shown: false`, so the bar can eventually be derived from settled results instead
    # of guessed from a hand-tuned score.
    probe: list[TradeTicket] = field(default_factory=list)
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


# A position worth a fifth of everything a wallet has open is a concentrated bet by any
# standard, so that is where "fully committed" is pinned. The sqrt curve matches the
# dollar term's shape: most of the credit arrives early, the tail flattens.
_FULL_COMMITMENT_FRACTION = 0.20

# What a profitable record in some OTHER category is worth on this market. Not zero -
# the wallet has genuinely made money and the leaderboard is only a top-N slice, so a
# real specialist can be missing from their own board. Not one either: general skill is
# weaker evidence than skill at the question actually being asked.
_OFF_CATEGORY_CREDIT = 0.5

# Below this share, the flow is genuinely spread across wallets and reads as agreement.
# Above it, one trader IS the signal - and in an election market that is the documented
# failure mode rather than the evidence. Pinned high on purpose: this rule is meant to
# fire on the lopsided cases, not to quietly tax every politics ticket.
_ELECTION_CONCENTRATION_FLOOR = 0.60
# What a fully one-wallet election signal keeps. Not zero - a large informed bet is still
# information, and the wallet did earn its leaderboard place. Halved, because we cannot
# tell that information apart from someone simply moving a thin market.
_CONCENTRATED_ELECTION_CREDIT = 0.5


def _concentration(wallet_usd) -> float | None:
    """Largest single wallet's share of this signal's notional, or None if unknowable.

    None, not 1.0, when there is nothing to measure: an unreadable breakdown must not
    read as "one wallet did all of it", which would apply the harshest penalty on the
    least evidence.
    """
    sizes = [usd for _addr, usd in wallet_usd or () if usd > 0]
    total = sum(sizes)
    return (max(sizes) / total) if total > 0 else None


def _bankroll_fraction(wallet_usd, bankrolls: dict[str, float]) -> float | None:
    """How much of themselves these wallets put on this side, notional-weighted.

    Per wallet, not pooled: pooling lets one $10M wallet's rounding error drown out the
    $100k wallet that went all in, which is the exact confusion this is here to fix.
    Wallets whose bankroll we could not read contribute nothing at all - a missing
    denominator is not a small denominator.
    """
    num = den = 0.0
    for addr, usd in wallet_usd or ():
        bankroll = bankrolls.get(addr)
        if not bankroll or bankroll <= 0 or usd <= 0:
            continue
        # Capped at 1: a fill can exceed the book we can see (they may have since sold),
        # and "more than everything they have" is not a stronger statement than "all of it".
        num += usd * min(1.0, usd / bankroll)
        den += usd
    return (num / den) if den > 0 else None


def _conviction(*, n_wallets: int, usd: float, minutes_ago: float, drift_c: float,
                hours_to_resolve: float | None, liquidity: float,
                bankroll_fraction: float | None = None,
                fresh: bool = False, category: str = "",
                n_category_matched: int = 0, is_election: bool = False,
                concentration: float | None = None) -> tuple[float, list[str]]:
    """Blend the signal's components into 0..1, and explain the blend in words.

    Deliberately simple and legible: every term is something you could check by hand.
    It ranks ideas against each other - it is not a probability of winning.
    """
    why: list[str] = []
    score = 0.0

    # "Who is behind this" has to mean who is behind it *at this question*. A wallet
    # that got rich on soccer is not an authority on a Senate race, and pooling the
    # leaderboards scored them identically. Off-category wallets still count, at a
    # discount. An unknown category (no `category`) applies no rule at all - guessing
    # a market's domain and then judging expertise against the guess is two errors.
    if category and not fresh:
        off = max(0, n_wallets - n_category_matched)
        effective = n_category_matched + _OFF_CATEGORY_CREDIT * off
    else:
        effective = float(n_wallets)
    agree = min(1.0, effective / 3.0)

    # Elections inverse the usual reading of concentration. Everywhere else, size from
    # few wallets is strong evidence. Here it is evidence the PRICE may be distorted:
    # a single trader held 2024 pricing away from the polls for weeks, and copying that
    # buys the distortion, not the information. Same input, opposite sign, one category.
    #
    # One-directional by construction - it can only cut ``agree``, never raise it - so a
    # market we merely failed to classify as an election is scored exactly as before.
    if (is_election and concentration is not None
            and concentration > _ELECTION_CONCENTRATION_FLOOR):
        over = ((concentration - _ELECTION_CONCENTRATION_FLOOR)
                / (1.0 - _ELECTION_CONCENTRATION_FLOOR))
        agree *= 1.0 - over * (1.0 - _CONCENTRATED_ELECTION_CREDIT)
        why.append(f"election market and {concentration * 100:.0f}% of this is one "
                   "wallet — concentration here can move the price rather than confirm it")

    score += 0.30 * agree
    if fresh:
        # Say what it actually is. Calling an unranked wallet "profitable" would be a
        # plain lie in the one line a human reads before risking money, and the whole
        # point of this signal is that we know nothing about who placed it.
        why.append("new wallet with no track record — pattern signal, not a copy")
    else:
        why.append(
            f"{n_wallets} profitable wallet{'s' if n_wallets != 1 else ''} on this side")

    # $10k is the floor to count at all; $200k+ is as convincing as size gets.
    size = min(1.0, (usd / 200_000.0) ** 0.5)
    # ...but dollars alone can't tell a real opinion from a rounding error: $50k is half
    # of a $100k wallet and 0.5% of a $10M one. So the term carries two independent
    # pieces of evidence - the bet is big, AND it is big *to them* - and needs both.
    # They multiply: a trade that is a rounding error to the wallet that placed it is
    # not made convincing by having lots of zeroes.
    #
    # Deliberately one-directional. ``commitment`` tops out at 1.0, so this can only cut
    # the size term, never raise it. That matters because the free API cannot see idle
    # USDC: a wallet sitting in cash looks smaller than it is and its positions look
    # proportionally bigger, and that blind spot must never manufacture conviction.
    # Unknown bankroll lands in the same place - commitment 1.0, score unchanged.
    if bankroll_fraction is not None:
        commitment = min(1.0, (bankroll_fraction / _FULL_COMMITMENT_FRACTION) ** 0.5)
        if commitment < 1.0:
            why.append(f"${usd:,.0f} — but only {bankroll_fraction * 100:.1f}% of their book")
        else:
            why.append(f"${usd:,.0f}, {bankroll_fraction * 100:.0f}% of their own book")
        size *= commitment
    else:
        why.append(f"${usd:,.0f} of smart money behind it")
    score += 0.25 * size

    # NOT named ``fresh``: that is the new-wallet flag, a bool, and this is a recency
    # score. Rebinding the parameter worked only because every bool read happened to sit
    # above this line - a booby trap for the next edit, which would silently get 0.83
    # where it asked "is this an unproven wallet".
    recency = max(0.0, 1.0 - minutes_ago / 180.0)
    score += 0.15 * recency
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


def _fresh_verdicts(cfg, signals, metas, prof, now) -> dict[tuple, str]:
    """Vet fresh-wallet candidates, cheap gates first. {key: rejection reason}.

    The claim being tested is specific - a NEW wallet taking a LARGE ONE-SIDED position
    in an ILLIQUID market - and each clause has to hold on its own. Size and one-
    sidedness were settled upstream; liquidity and age are settled here, in that order,
    because age is the only one that costs an HTTP call and there is no reason to spend
    it on a market that was never thin enough to qualify.
    """
    fresh = [s for s in signals if s.fresh]
    if not fresh:
        return {}
    verdict: dict[tuple, str] = {}
    todo = []
    for s in fresh:
        m = metas.get(s.market_id)
        if not m:
            continue          # the main loop's own metadata check owns this rejection
        liq = float(m.get("liquidity") or 0.0)
        # Unknown liquidity is not thin liquidity. The pattern is specifically about
        # informed money moving a market too small to absorb it; without the depth
        # number that claim is unverifiable, so it fails rather than passes.
        if liq <= 0:
            verdict[_key(s)] = "fresh wallet: market depth unknown"
        elif liq > cfg.whale_fresh_max_liquidity:
            verdict[_key(s)] = "fresh wallet: market too liquid for the pattern"
        else:
            todo.append(s)
    if not todo:
        return verdict
    if prof is None:
        for s in todo:
            verdict[_key(s)] = "fresh wallet: age unavailable"
        return verdict
    try:
        firsts = prof.first_seen([s.wallet_usd[0][0] for s in todo if s.wallet_usd])
    except Exception:  # noqa: BLE001
        firsts = {}
    for s in todo:
        addr = s.wallet_usd[0][0] if s.wallet_usd else ""
        first = firsts.get(addr)
        if not first:
            # Fail closed. "We could not read this wallet's history" must never be
            # allowed to mean "this wallet has no history", which is the single
            # assumption that would turn every unreadable address into a buy signal.
            verdict[_key(s)] = "fresh wallet: age unavailable"
            continue
        if (now - first) / 86400.0 > cfg.whale_fresh_max_age_days:
            verdict[_key(s)] = "wallet not new (no track record either)"
    return verdict


def _key(s) -> tuple:
    return (s.market_id, s.outcome, s.wallet_usd[0][0] if s.wallet_usd else "")


def build_board(cfg, client, scorer, account: Account, *,
                now_ts: float | None = None, meta_fetch=None,
                profiler=None) -> BoardReport:
    """Whale flow -> filtered, ranked, human-placeable tickets."""
    from .gamma import market_meta, outcome_price
    from .whales import build_wallet_profiler

    report = BoardReport()
    now = time.time() if now_ts is None else now_ts
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)

    try:
        signals, exits = scan_smart_flow(
            client, scorer,
            categories=cfg.copytrade_categories,
            min_usd=cfg.copytrade_min_usd,
            min_wallets=cfg.copytrade_min_wallets,
            max_price=cfg.copytrade_max_price,
            limit=cfg.copytrade_feed_limit,
            time_periods=cfg.copytrade_time_periods,
            leaderboard_limit=cfg.copytrade_leaderboard_limit,
            track_exits=getattr(cfg, "whale_track_exits", False),
            fresh_min_usd=(cfg.whale_fresh_min_usd
                           if getattr(cfg, "whale_fresh_enabled", False) else None),
            now_ts=now,
        )
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"whale feed unavailable: {exc}")
        return report

    # Keyed by the exact (market, outcome) a ticket would buy. Deliberately not matched
    # across outcomes: on a two-way market a sell of the other leg looks like support
    # for this one, but on a multi-outcome market it means nothing at all, and inventing
    # a signal from that ambiguity is worse than staying quiet.
    exit_by_leg = {(x.market_id, x.outcome.strip().lower()): x for x in exits}

    report.considered = len(signals)
    if not signals:
        report.notes.append("no qualifying smart-money buys in the current feed")
        return report

    fetcher = meta_fetch or market_meta
    unread: set[str] = set()
    try:
        try:
            metas = fetcher([s.market_id for s in signals], failures=unread)
        except TypeError:      # an injected test fetcher need not take the kwarg
            metas = fetcher([s.market_id for s in signals])
    except Exception:  # noqa: BLE001
        metas = {}
        unread = {s.market_id for s in signals}

    # One batched, cached sweep for every wallet in the feed - not one call per ticket.
    # An outage here costs the bankroll refinement, not the board: an empty map means
    # every signal falls back to flat dollar sizing, which is what it did before.
    bankrolls: dict[str, float] = {}
    prof = profiler if profiler is not None else build_wallet_profiler(cfg, client)
    if prof is not None and getattr(cfg, "whale_bankroll_weighting", False):
        try:
            bankrolls = prof.bankrolls(
                [addr for s in signals for addr, _usd in s.wallet_usd])
        except Exception as exc:  # noqa: BLE001
            report.notes.append(f"bankroll data unavailable ({exc}) — flat dollar sizing")
            bankrolls = {}

    fresh_verdict = _fresh_verdicts(cfg, signals, metas, prof, now)

    candidates: list[TradeTicket] = []
    for s in signals:
        # A parlay, not a market. Polymarket sells multi-leg combos through the same
        # trade feed, carrying a SYNTHETIC id (0x + 62 hex, zero-padded), no slug, and a
        # title that is several questions joined by " AND ". Gamma has no such market
        # because there is no such market, so these arrived as "no market metadata" and
        # read as a lookup problem. They are not one, and copying them would be wrong
        # even if they resolved cleanly: a combo's legs are correlated, its price is not
        # the product of its parts, and none of the drift or resolution filters below
        # mean anything applied to a basket.
        # Judge only ids that CLAIM to be on-chain keys. Anything else falls through to
        # the metadata check below, which already fails closed - this rule exists to
        # name a specific malformation, not to become a second gate on market naming.
        mid = s.market_id or ""
        if mid.startswith("0x") and not _CONDITION_ID.fullmatch(mid):
            report.reject("parlay / combo ticket, not a single market")
            continue

        m = metas.get(s.market_id)

        # Fail CLOSED on missing metadata. Without it there is no end date and no game
        # start, so every date filter below would silently pass - "we know nothing" must
        # never read as "nothing is wrong". A dropped metadata batch once let the whole
        # board through unchecked before dying at the pricing step.
        #
        # But say WHICH kind of missing. "Gamma has no such market" is a signal to drop;
        # "we could not reach Gamma" is a fault to fix, and filing both under one reason
        # made a leak look like a filter. Same outcome for the ticket, opposite meaning
        # for anyone reading the ledger to decide what to work on next.
        if not m:
            report.reject("market lookup failed (not read)" if s.market_id in unread
                          else "no market metadata")
            continue

        if m.get("closed") or not m.get("active", True):
            report.reject("market closed")
            continue

        # Esports, cut on measured evidence - see `Config.esports_enabled`. Placed AFTER
        # the metadata fetch on purpose: Gamma's `question` and `slug` are what the
        # classifier can trust, and a derivative market on an esports fixture ("Game
        # Handicap: NS (-1.5) vs DN SOOPers (+1.5)") names no game anywhere except the
        # event slug. Rejecting by reason rather than skipping silently keeps the count
        # visible on the board, so re-arming can be judged against what it lets back in.
        if not getattr(cfg, "esports_enabled", False) and is_esports(
                m.get("question") or s.title, s.event_slug or m.get("slug", "")):
            report.reject("esports (sleeve retired — see esports_enabled)")
            continue

        if s.fresh:
            reason = fresh_verdict.get(_key(s))
            if reason:
                report.reject(reason)
                continue

        hours = _hours_until(m.get("end_date", ""), now_dt)
        if hours is not None and hours <= 0:
            report.reject("already resolved")
            continue

        start_h = _hours_until(m.get("game_start", ""), now_dt)
        if start_h is not None and start_h <= 0:
            report.reject("in-play (event already started)")
            continue
        if start_h is None and s.category == "SPORTS":
            # We could not tell whether the ball is already in the air. For a scheduled
            # fixture that is a refusal, not a pass - in-play is the exact shape the
            # June-26 post-mortem was written about. Counted separately so the ledger
            # never again reports "in-play" for markets that simply have no kickoff.
            report.reject("sports market with no event start time")
            continue

        # `end_date` is a SETTLEMENT deadline and on sports it is padded hard: a
        # same-night Mets-Pirates game carried an end_date seven days out, and three
        # tennis tickets all reported an identical 165.9h. Time-until-the-event is the
        # honest horizon for anything about whether a signal is still fresh or still
        # actionable, so derive one and use it for those gates. `hours` keeps its
        # end_date meaning for "already resolved" and the max-days capital check.
        event_h = hours if start_h is None else (
            start_h if hours is None else min(start_h, hours))
        if event_h is None:
            # No end date and no kickoff. Every gate below is horizon-scaled, so this
            # market would sail past all of them on the strength of a missing field -
            # and `_max_signal_age(None)` hands it the floor allowance, i.e. the least
            # scrutiny for the case we know the least about. Not knowing when something
            # resolves is a reason to pass on it.
            report.reject("no resolution or start time")
            continue

        # Deliberately `hours`, not `event_h`. This gate is about time to RESOLUTION,
        # and the config comment on `board_min_hours` says so: in-play is the separate
        # `game_start` check above, and folding the two together is what that comment
        # warns against. A game kicking off in 1h but settling in 4h is a normal
        # pre-game ticket and must stay one.
        if hours is not None and hours < cfg.board_min_hours:
            report.reject(f"resolves in under {cfg.board_min_hours:.0f}h")
            continue

        if hours is not None and hours > cfg.board_max_days * 24:
            report.reject(f"resolves more than {cfg.board_max_days:.0f} days out")
            continue

        # Staleness scales with the horizon, so feeding it the padded deadline is what
        # disabled it where it mattered most: at 165.9h the allowance became 1,991
        # minutes, i.e. the board would accept a 33-hour-old whale fill on a match
        # starting today. Anchored to the event, that same ticket gets minutes.
        if s.minutes_ago > _max_signal_age(cfg, event_h):
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
        # Drift was never the whole of what we pay away versus the whale. The taker fee
        # is gone the moment the fill lands, exactly like drift, and this gate ignored
        # it - so under a 4.0c cap a ticket could sit 5.75c worse than the trade it
        # claimed to copy. `copy_give_up_c` charges the fee against the same budget; the
        # budget itself is untouched.
        #
        # It is the SAME fee the row will be booked at (`papertrial.record` uses
        # `cfg.fee_multiplier` for anything without an index override), because a bar
        # tested against one schedule and settled against another is not a bar.
        #
        # Over the 293 published opinions this refuses 7: a 28.6% win rate and -$375,
        # against 54.9% and +$394 for what it keeps. The headline moves from -0.79 to
        # -0.21 points per bet. That is a measurement of what the gate would have done,
        # not a claim it will do it again - and no published row changes, since the
        # trial is append-only and these seven are already settled and recorded.
        if copy_give_up_c(entry, s.avg_price,
                          multiplier=cfg.fee_multiplier) > cfg.board_max_drift_c:
            report.reject("price + fee ran away from the whale's entry")
            continue

        conviction, why = _conviction(
            n_wallets=s.n_wallets, usd=s.total_usd, minutes_ago=s.minutes_ago,
            drift_c=drift_c, hours_to_resolve=event_h,
            liquidity=float(m.get("liquidity") or 0.0),
            bankroll_fraction=_bankroll_fraction(s.wallet_usd, bankrolls),
            fresh=s.fresh,
            # Passing "" turns the category rule off entirely, which is what both the
            # flag and an unclassifiable title have to do: no category means no basis
            # for judging whether the record behind this trade is the relevant one.
            category=(s.category if getattr(cfg, "whale_category_match", False) else ""),
            n_category_matched=s.n_category_matched,
            is_election=(s.is_election and getattr(
                cfg, "whale_election_concentration_penalty", False)),
            concentration=_concentration(s.wallet_usd),
        )
        # Below the probe floor is genuinely not worth carrying. Between the probe floor
        # and the buy bar the ticket is RECORDED but not recommended - see the split
        # below `best`, and the note on `board_probe_min_conviction` in config.py.
        if conviction < cfg.board_probe_min_conviction:
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
        if s.fresh:
            warnings.append("unproven wallet — this is a pattern, not a track record")
        elif s.n_wallets == 1:
            warnings.append("only one wallet — single opinion")

        # The record exists, but not at this question. Worth saying out loud rather than
        # only burying it in the score: "profitable wallet" reads as authority, and on a
        # tennis market a crypto specialist's is borrowed authority.
        if (getattr(cfg, "whale_category_match", False) and s.category
                and not s.fresh and s.n_category_matched == 0):
            warnings.append(
                f"no {s.category.lower()} track record — these wallets earned their "
                "record elsewhere")

        conc = _concentration(s.wallet_usd)
        if (getattr(cfg, "whale_election_concentration_penalty", False) and s.is_election
                and conc is not None and conc > _ELECTION_CONCENTRATION_FLOOR):
            warnings.append(
                f"{conc * 100:.0f}% of this signal is a single wallet on an election "
                "market — that is the shape that distorted 2024 pricing")

        # Smart money leaving the exact side we are about to recommend. Only flagged
        # when the sell is MORE RECENT than their latest buy: a wallet that trimmed
        # before adding is scaling in, and calling that an exit would cry wolf on every
        # position that was ever partly sold.
        ex = exit_by_leg.get((s.market_id, s.outcome.strip().lower()))
        if ex is not None and ex.minutes_ago < s.minutes_ago:
            warnings.append(
                f"smart money sold ${ex.total_usd:,.0f} of this side "
                f"{ex.minutes_ago:.0f} min ago ({ex.n_wallets} wallet"
                f"{'s' if ex.n_wallets != 1 else ''}) — after the buys above")
        if sizing.capped_by == "per-market-limit":
            warnings.append("size capped by the per-market contract limit")

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
            hours_to_resolve=None if event_h is None else round(event_h, 1),
            n_wallets=s.n_wallets,
            whale_usd=round(s.total_usd),
            minutes_ago=round(s.minutes_ago),
            liquidity=round(float(m.get("liquidity") or 0.0)),
            url=event_url(s.event_slug) or event_url(m.get("slug", "")),
            end_iso=m.get("end_date", ""),
            event_iso=m.get("game_start", ""),
            slug=s.slug or m.get("slug", ""),
            signal_ts=now - s.minutes_ago * 60.0,
            wallet_usd=tuple((addr, round(float(usd), 2))
                             for addr, usd in (s.wallet_usd or ())),
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

    ranked = sorted(best.values(), key=lambda c: c.conviction, reverse=True)

    # WHAT TO BUY IS A BAR, NOT A QUOTA. A count cap answers "how many will fit on the
    # page", which has nothing to do with whether the ninth ticket is worth money: on a
    # busy day it silently dropped tickets stronger than the ones published on a quiet
    # one. The bar is `board_min_conviction`; `board_max_tickets` survives only as an
    # opt-in safety valve (0 = off) for capping exposure on an unusually loud day.
    report.tickets = [t for t in ranked if t.conviction >= cfg.board_min_conviction]
    report.probe = [t for t in ranked if t.conviction < cfg.board_min_conviction]
    for _ in report.probe:
        report.reject(f"below the {cfg.board_min_conviction:.2f} buy bar (recorded, not bought)")

    if cfg.board_max_tickets > 0 and len(report.tickets) > cfg.board_max_tickets:
        # Counted, never silent. Truncating without a ledger entry is what made
        # `considered` minus the rejections fail to equal the published count on the
        # 2026-08-07 board - 17 qualified, 8 shipped, and nothing said where 9 went.
        overflow = report.tickets[cfg.board_max_tickets:]
        report.tickets = report.tickets[: cfg.board_max_tickets]
        report.probe = overflow + report.probe
        for _ in overflow:
            report.reject(f"over the {cfg.board_max_tickets}-ticket cap")
    if not report.tickets and not report.notes:
        report.notes.append(
            "Signals came in but none cleared the filters — that is a normal, and "
            "usually correct, outcome."
        )
    return report
