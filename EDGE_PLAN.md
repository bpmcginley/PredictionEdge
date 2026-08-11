# PredictionEdge — Edge Expansion Plan (Tiers 1–3)

Written 2026-08-06. `PLAN.md` covers making the *existing* machine correct and unattended.
This document covers making it **find more, and better, edges** — the whale copy layer is
currently the only signal feeding the board, and one signal is a single point of failure.

Each workstream is scoped to be handed to one agent, with **disjoint file ownership** so
several can run at once without stepping on each other.

---

## 0. Constraints that shape every choice below

These are not preferences. They eliminate entire categories before design starts.

1. **Advisory only.** Omen prohibits API access, bots, browser automation and
   signal-following services. `research_only=True` makes `LiveExecutor` refuse to
   construct. **No workstream here may add an order path.** Everything emits *tickets a
   human reads*.
2. **Manual placement ⇒ no speed edges.** Anything requiring sub-5-minute reaction is
   out: release-moment trading, stale-quote sniping, most microstructure. What survives
   is slow, structural mispricing that persists for hours or days.
3. **Omen prices off GLOBAL Polymarket** (explicitly not Polymarket US). Kalshi-only
   series — weather, many econ brackets — are **not tradeable**, however good the signal.
   Kalshi is a *reference*, never a venue. Any ticket must be placeable on global
   Polymarket.
4. **Free data only.** No paid feeds.
   *Corrected 2026-08-06:* an earlier draft of this document asserted the Odds API quota
   was exhausted and "not coming back on the free tier." **That was wrong.** The free tier
   is 500 credits/month and **resets monthly** — measured live at 490/500 remaining. It
   also carries **Pinnacle** (under `regions=eu`, not the `us` default `odds.py` sends),
   so the sharpest public reference is free. The sports de-vig path was never dead; it was
   misconfigured and over-polled. See E3b.
5. **Fail closed.** The `market_meta` incident (2026-08-05) is the standing lesson: a
   silently-empty upstream let every date filter pass unchecked. Missing data must
   reject, never wave through.

---

## 1. Ownership map (so agents don't collide)

| Workstream | Owns (creates/edits) | Must not touch |
|---|---|---|
| **E1 Consistency** | `consistency.py`, `tests/test_consistency.py` | anything else |
| **E2 Crypto RND** | `deribit.py`, `tests/test_deribit.py`, `crypto.py` | `signals.py`, `copytrade.py` |
| **E3 Macro FV** | `macrofv.py`, `tests/test_macrofv.py` | `signals.py`, `crypto.py` |
| **E4 Whale depth** | `copytrade.py`, `whales.py`, `signals.py`, their tests | new modules above |

`config.py` and `.env.example` are **pre-populated with every knob all four need**, before
any agent starts. No agent edits config — that file is the one guaranteed collision point.

Integration (wiring new signal sources into the board, `publish.py`, the site, commits) is
done centrally *after* the agents land, not by the agents.

---

## Tier 1 — Mechanical edges (no forecast required)

The best kind: being right about the world is not a prerequisite.

### E1 — Logical-consistency scanner  ⟵ *highest value, build first*

**New module: `consistency.py`**

Prediction markets contain hard logical constraints that must hold and frequently don't.
Three families:

1. **Ladder monotonicity.** P(BTC > $150k) ≤ P(BTC > $120k). P(Fed cuts ≥50bp) ≤
   P(Fed cuts ≥25bp). Parse the numeric threshold out of each market in a group, sort,
   and flag any pair where the higher threshold prices *above* the lower one.
2. **Bracket sums.** For a mutually-exclusive set: if Σ(asks) < 1.00 you can buy the whole
   set for a guaranteed payout of 1.00. If Σ(bids) > 1.00 the reverse. Polymarket's
   `negRisk` flag marks exactly these grouped sets.
3. **Dominance.** P(X wins presidency) ≤ P(X wins nomination). P(X is nominee) ≤
   P(X runs). A strictly-harder event cannot be more likely than its prerequisite.

**Why this succeeds where `crossvenue.py` failed.** That module produced confident false
edges — "run for" vs "win" read as a 48¢ gap — because it matched fuzzy titles *across
venues* with nothing to anchor them. Here, **the venue itself asserts the relationship**:
markets share an event slug, appear in one `negRisk` group, and carry structured
`floor_strike`/`cap_strike` fields. The matching problem that sank cross-venue largely
disappears. Reuse `crossvenue.question_conflict()` as a *guard* on the dominance family
only — it is exactly the right tool for "are these actually the same question."

**Deliberate limits:**
- Report the violation in cents with both legs named. Never assert a violation you cannot
  price on both sides — an unpriceable leg is a rejection, not an assumption.
- Both legs must be on **global Polymarket** or the ticket is unplaceable on Omen.
- A violation smaller than the round-trip spread is not an edge. Net it against the spread
  before reporting.

**Done when:** `python -m predictionedge.consistency --show-rejected` prints live
violations with both legs, the constraint that was broken, and the net-of-spread gap; and
a hand-checkable synthetic fixture proves each of the three families fires.

### E1b — Deadline decay & longshot fade (measurement only, no tickets yet)

Folded into E1 as a **research probe**, not a ticket source.

- "Will X happen by [date]" systematically overprices the event as the deadline nears with
  no progress.
- Sub-5¢ contracts are persistently overpriced; buying NO wins ~95% of the time.

**Longshot fade is deliberately NOT shipped as a ticket source.** Its payoff shape — many
small wins, rare large loss — is actively hostile to Omen's 5% daily loss limit. That
combination is the classic prop-account blow-up. Measure the effect, print the finding,
ship nothing until there is settled-trade evidence and a sizing rule that respects the
drawdown cap.

---

## Tier 2 — Free fair-value references that beat the crowd

### E2 — Crypto: replace realized-vol lognormal with Deribit's implied distribution

**New module: `deribit.py`; rewires `crypto.py`.**

The current model reads 0.91 where the market reads 0.98. A gap that size against a liquid
market is nearly always **model error, not edge** — and the cause is identifiable: it uses
*realized* volatility as a stand-in for the *risk-neutral* distribution, with zero drift.
Those are different objects.

Deribit publishes the full BTC/ETH options chain free and unauthenticated. From it:

- Pull the option chain per expiry.
- Recover the risk-neutral density (Breeden–Litzenberger: the second derivative of call
  price with respect to strike), or, more robustly for our purpose, read
  **P(S_T > K) directly as −∂C/∂K** — a digital's price is the negative slope of the call
  curve, which is exactly what a threshold market pays.
- Interpolate across strikes; interpolate in *variance* across the two bracketing expiries
  when the market's date falls between listed expiries.

This is the deepest crypto vol market that exists. It gives WS-E (the calibration gate in
`PLAN.md`) a fair test — that gate is currently being asked to judge a model we already
have reason to believe is misspecified.

**Deliberate limits:**
- BTC/ETH only. Deribit has no meaningful SOL/XRP chain; those stay on the old model and
  must be *labelled* as such, not silently mixed.
- Wide/crossed/stale option quotes must be discarded, and a strike ladder with holes must
  fail closed rather than interpolate across a gap.
- **Stays paper.** This changes the model, not the promotion gate.

**Done when:** for a live BTC threshold market, the module prints the Deribit-implied
probability beside the market price and the old lognormal number, and a fixture-based test
proves the digital extraction against a hand-computed chain.

**Outcome, measured 2026-08-07 — Deribit cannot price Kalshi's crypto complex.** The
module is correct; the venues do not overlap in time. Every open market across
`KXBTCD`/`KXETHD`/`KXBTC`/`KXETH` resolves in **0.03 or 0.20 days** — two intraday
expiries, nothing beyond a day — while Deribit's **front** expiry sits ~0.66 days out.
So every Kalshi crypto market falls *before* the front, where there is no lower bracket
to interpolate from and extrapolating would be inventing a number, which `deribit.py`
rightly refuses.

Two corrections this forces:

- The earlier suggestion that Deribit "earns its keep at weekly-plus tenors excluded by
  `crypto_max_days=10`" was **wrong in both halves**. The cap is not binding —
  `resolves beyond 10d` counts **zero** — so widening it admits no markets at all. The
  binding filter is `crypto_min_hours=1`, at the *short* end (663 drops).
- Kalshi's genuinely longer-dated crypto series (`KXBTCMAXM`, `BTCMINMAXY`,
  `KXETHMINMON`) are **max/min/one-touch** markets. Those are path-dependent barriers,
  and a terminal risk-neutral density is the wrong object for them. They are not a way
  to reach the tenor Deribit covers.

What shipped instead is **visibility**: `DeribitPricer.coverage()` names why a date
cannot be priced, and `find_crypto_edges(..., report=)` tallies every drop, so an empty
crypto scan distinguishes "could not price" from "priced and found no edge". Live now:
663 inside the 1h floor, 50 priced with no edge, 12 before Deribit's front expiry.

### E3 — Macro: Fed funds futures + Cleveland Fed nowcast

**New module: `macrofv.py`.**

Two independent, free, genuinely-better-than-retail references:

1. **Fed decisions ← fed funds futures.** The CME rate complex is among the deepest
   markets on earth. Implied probabilities from front-month contracts are the reference
   price for every "Fed cuts/hikes at the [month] meeting" market. When Polymarket
   disagrees with the futures strip, the strip is right.
2. **CPI ← Cleveland Fed inflation nowcast.** Published daily, free, and materially more
   accurate than retail guesswork. CPI bracket markets that disagree with it are a clean
   model edge.

Verify the exact free endpoints before building — publish routes for both have moved
historically, and FRED is the reliable fallback host for the futures series. **If a source
cannot be reached without a key, report that and stop; do not substitute a scraped
approximation.**

**Deliberate limits:**
- A nowcast is a point estimate; converting it to bracket probabilities needs an explicit
  uncertainty assumption. Make that assumption a named, documented constant, not a magic
  number buried in a formula.
- Fed markets on Polymarket sometimes resolve on wording ("cuts by 25bp *or more*") that
  does not match the futures contract. Match the *claim*, not the headline.

**Done when:** the module prints, for each live Polymarket Fed/CPI market it can match,
the market price beside the reference-implied probability and the gap.

### E3b — Sports: sharp closing lines (research spike, then decide)

The de-vig code in `devig.py` and `odds.py` already works and is sitting idle purely
because the Odds API free quota is gone. The closing line at a sharp book is the single
best public predictor in sports, and this is **existing code that only needs a feed** —
the cheapest possible win if a free source exists.

Scoped as a **research spike**: find whether a genuinely free, terms-compliant odds source
exists with enough coverage to matter. Report findings. Do not build a scraper that
violates a site's terms — that is a hard stop, report and move on.

---

## Tier 3 — Depth in the whale layer we already own

All four changes below live in code that already exists and is already trusted.

### E4 — Whale signal depth

**Edits `copytrade.py`, `whales.py`, `signals.py`.**

1. **Bankroll fraction, not dollar size.** *(highest value — smallest change)*
   $50k from a wallet holding $10M is noise. $50k from a wallet holding $100k is a real
   opinion. `_conviction()` currently scores these identically via a flat
   `(usd / 200_000) ** 0.5` curve. Weight by the position's share of that wallet's
   bankroll. This distorts the number that decides *every* ticket's rank and size.

2. **Wallet exits.** Only BUYs are tracked (`if t.side != "BUY": continue`). A smart wallet
   *dumping* is at least as informative as one entering, and nothing is watching it. At
   minimum: a smart-money exit from a market currently on the board is a warning on that
   ticket.

3. **Fresh-wallet concentration.** New wallet funds up and immediately takes a large,
   one-sided position in an illiquid market. That is the documented shape of informed flow
   on Polymarket — appointments, award shows, geopolitical events. It is **structurally
   invisible to the leaderboard**, since a wallet with no history cannot rank. This finds
   signals the current system *cannot* see, rather than re-ranking ones it already has.

4. **Category-matched competence.** A politics whale is not a soccer whale; today they
   score the same. Require category-matched track record (already scoped as WS-C in
   `PLAN.md`).

5. **Election concentration inversion.** Election markets have a documented
   whale-distortion problem — the French trader who moved 2024 pricing is the known case.
   In that category, heavy single-wallet concentration is a reason for *less* confidence in
   the price, not more. That is the **exact inverse** of how `_conviction()` treats
   concentrated smart money today. Encode it as a category-specific rule.

**Deliberate limits:**
- Every change here moves `_conviction()`, which drives ranking *and* position size. Each
  sub-change needs a test pinning the behaviour it claims, and the existing 176 tests must
  stay green — several of them encode what the June-26 live session cost us.
- Recalibration against settled trades is **not** possible yet (the journal needs ~20–30
  settled trades). So: no tuning weights to make the current board look better. That is
  fitting to eight samples.

---

## What is deliberately NOT being built

Recording these so they don't get re-proposed:

- **News sentiment as a confidence multiplier.** Human-read context only. LLM-scored news
  reliably manufactures confidence, and it would feed the single number that sets sizing.
- **Cross-venue "arbitrage."** Under advisory-only you cannot trade both legs, so it was
  never arbitrage — it is a second opinion. Already demonstrated to produce confident false
  positives; `crossvenue.py` stays unwired.
- **Anything sub-5-minute.** Manual placement forecloses it.
- **Kalshi-native tickets.** Not placeable on Omen. Kalshi stays a reference source.

---

## Sequencing

1. Config knobs + `.env.example` land first, centrally. (Removes the one collision point.)
2. E1, E2, E3, E4 run in parallel — disjoint files.
3. Integration: wire new sources into the board behind flags, extend `publish.py`, surface
   on the site, run the full suite, commit.
4. Only then: promotion gates. Nothing new reaches a ticket the user acts on until it has
   printed sane numbers against live data and been eyeballed.
