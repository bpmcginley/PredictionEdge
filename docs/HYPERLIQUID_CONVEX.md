# Hyperliquid "occasional hit" strategy — research note

Written 2026-09-03. Research only; nothing here places an order. Companion script:
`scripts/hl_convex_sim.py` (stdlib, prints every table quoted below).

## TL;DR

The idea is a **convex ticket**: risk a small, fixed amount of isolated margin at high
leverage, lose most tickets small and fast, and let the rare ticket that catches a
real move run to a multiple of the stake. On Hyperliquid this is feasible because
isolated margin caps the loss at the margin posted, there is no liquidation fee, stops
trigger on a wick-resistant mark price, and entry + take-profit + stop can be sent as
one atomic order group.

Three findings decide the design:

1. **Leverage does not create the hit; it only sets the knock-out distance.** For BTC
   the maintenance margin is fixed by the tier (1.25%), so at 40x you are liquidated by
   a 1.25% adverse move, at 20x by 3.75%, at 10x by 8.75%. The payoff multiple comes
   from *letting a winner run*, not from the leverage number.
2. **Fees are the silent killer.** Round-trip taker fees plus slippage cost about 5% of
   the stake per ticket at 40x and 2.6% at 20x. With no directional edge the ticket
   loses 4–6% of the stake on average. Everything hinges on the trigger having real
   short-horizon directional edge.
3. **The only places with documented short-horizon directional edge in BTC are
   forced-flow moments**: scheduled macro releases (CPI, FOMC, payrolls), liquidation
   cascades, and volatility-conditioned intraday momentum. Random scalping and 1-minute
   options do not qualify.

Recommendation: build **EVENT-CONVEX** (macro-release impulse rider) first, with the
**CASCADE-RIDER** as the second trigger on the same infrastructure, at **20x on BTC**,
ticket = 1–2% of a ring-fenced sub-account, validated on testnet and then on a paper
track record before any live size.

---

## 1. Hyperliquid mechanics that matter (verified 2026)

| Item | Value | Why it matters here |
|---|---|---|
| Max leverage | BTC 40x, ETH 25x, SOL 20x, most alts 3–10x (tiered down by notional) | Only BTC/ETH support the leverage the idea needs |
| Initial margin at max leverage | BTC 2.5% (tier 1) | Stake per unit notional |
| Maintenance margin | Half of initial at the tier's max leverage → BTC 1.25% | Fixed by tier, **not** by your chosen leverage |
| Margin modes | Cross (default) or isolated | Isolated caps loss at the posted margin; use it |
| Liquidation trigger | Mark price, not last trade | Mark = median of (oracle + 150 s EMA basis), (median of best bid/ask/last), (median of CEX mids); updates ~every 3 s |
| Liquidation path | Market order to book first; if equity < 2/3 of maintenance, backstop via liquidator vault | On backstop the maintenance margin is **not** returned; a tight stop avoids this |
| Liquidation fee | None | Loss = margin, not margin + penalty |
| Taker / maker fee | 0.045% / 0.015% base, volume tiers, HYPE-staking and referral discounts | At 40x, one taker side = 1.8% of stake |
| Funding | Hourly, premium + clamp(interest − premium), **capped 4%/hour** | A squeeze can charge 4% of notional per hour = 160% of stake at 40x |
| Orders | Limit (GTC/IOC/ALO), market, stop/TP as trigger orders (market or limit), TWAP, reduce-only, `grouping="normalTpsl"` for atomic entry + TP + SL | Bracket the ticket in one call |
| Latency | Median ~0.2 s end-to-end, p99 ~0.9 s; blocks ~0.07 s | Fast enough for minute-scale, not for sub-second races |
| Rate limits | ~1,200 REST req/min per IP; ~100 exchange actions per 10 s per wallet; 10 WS subs per connection, no auto-reconnect | Ample for a ticket bot |
| Accounts | Agent (API) wallets without withdrawal rights; sub-accounts; testnet with faucet | Ring-fence the bankroll in a sub-account |

Position caps: $30M notional for max leverage ≥ 25, $5M for [20, 25), $2M for [10, 20).
Irrelevant at ticket size.

## 2. The geometry (from `hl_convex_sim.py`)

Distance to liquidation and round-trip cost for a BTC tier-1 ticket:

| Leverage | Liquidation distance | Fees + slippage, % of stake |
|---|---|---|
| 10x | 8.75% | 1.3% |
| 20x | 3.75% | 2.6% |
| 40x | 1.25% | 5.2% |

Probability of being knocked out by pure diffusion while holding (reflection
principle, driftless, annualized vol σ):

| Leverage | σ | 5 min | 15 min | 60 min | 240 min |
|---|---|---|---|---|---|
| 20x | 60% | 0.0% | 0.0% | 0.0% | 0.3% |
| 20x | 120% | 0.0% | 0.0% | 0.3% | 14.3% |
| 40x | 60% | 0.0% | 0.0% | 5.1% | 32.9% |
| 40x | 120% | 0.1% | 5.1% | 32.9% | 62.6% |

BTC 30-day realized vol was 27% in mid-August 2026 (VanEck), but the local vol inside
an event window or a cascade is routinely 3–5x that. Read the table at 60–120%.

What the table says: **40x is survivable only for holds under ~15 minutes; 20x
survives an hour even in a violent tape.** Diffusion is not the real danger anyway.
Jumps are: a 1% print against you at 40x is 80% of the stake in one tick, and the mark
price lags CEX spot by design, so you can be marked through your stop before the stop
fills.

Monte Carlo of a bracketed ticket (60-min window, local vol 90%, BTC):

| L | TP | SL | P(dir right) | hit | stop | EV on stake | Kelly |
|---|---|---|---|---|---|---|---|
| 40 | 2.0% | 0.8% | 0.50 (no edge) | 3% | 36% | **−4.3%** | — |
| 40 | 2.0% | 0.8% | 0.60 | 43% | 40% | +25% | 0.37 |
| 20 | 3.0% | 1.5% | 0.60 | 18% | 34% | +13% | 0.31 |
| 20 | 3.0% | 1.5% | 0.65 | 49% | 35% | +24% | 0.44 |
| 10 | 5.0% | 3.0% | 0.65 | 5% | 27% | +12% | 0.34 |

"Occasional hit" variant (no take-profit, trailing stop once in profit, 240-min
window, vol 60%, plus a ±1% jump at 0.5%/min):

| L | SL | trail | P(dir right) | P(win) | avg win | avg loss | EV on stake |
|---|---|---|---|---|---|---|---|
| 40 | 0.8% | 0.6% | 0.50 (no edge) | 31% | 0.35x | 0.25x | **−6.2%** |
| 40 | 0.8% | 0.6% | 0.55 | 46% | 1.39x | 0.30x | +48% |
| 20 | 1.5% | 1.0% | 0.60 | 56% | 1.23x | 0.24x | +58% |
| 20 | 2.0% | 1.5% | 0.60 | 59% | 1.41x | 0.35x | +69% |

Two cautions on those rows. The positive EVs are **entirely** the assumed edge (a 55–60%
direction call plus a persistent drift); they show the *shape* of the payoff, not that
the edge exists. And the no-edge rows are the honest baseline: fees plus stop
asymmetry lose 4–6% of every stake, so a trigger that is only slightly better than a
coin flip is still a losing machine.

Break-even hit rate for a ticket paying b:1 net is 1/(1+b): 25% at 3:1, 9% at 10:1.
Quarter-Kelly at ten points above break-even is 3–5% of bankroll per ticket; use 1–2%.

## 3. Where the hit could come from (ranked by evidence)

### 3a. Scheduled macro release impulse — build first

Evidence. BTC's 1-hour reaction to CPI surprises had an R² near 80% in 2026 (Block
Scholes), with the reaction concentrated in the first hour. Nazaruk (KSE, 2026) and
the ScienceDirect intraday-news study document pre-announcement volatility build-up and
announcement-hour moves for CPI and FOMC. Shen et al. (Financial Review, 2022) show
intraday momentum in BTC is strongest in the highest-volume/volatility sessions and is
driven by liquidity provision, i.e. the move continues because the other side is
slow to absorb it.

Mechanism. The release prints, spot on Binance/Coinbase jumps, Hyperliquid's mark
lags by a few seconds (oracle at ~3 s, 150 s EMA on the basis), and the impulse
continues for minutes to an hour as positioned leverage gets flushed.

Rules (starting point, to be fit on data):
- Calendar: CPI, core PCE, NFP, FOMC statement and presser, plus any surprise
  headline that moves the oracle by > 0.6% in 60 s.
- Direction: sign of the first 60 s move of the **oracle** price, confirmed by a
  liquidation burst on the same side (WS `trades` with the liquidation flag). Do not
  guess the direction before the print; that is a coin flip with 5% vig.
- Entry: IOC taker at 20x isolated, within 30–90 s of the print, only if the
  60 s move is between 0.4% and 1.5% (below: no impulse; above: you are the exit
  liquidity).
- Bracket in one `normalTpsl` group: stop at 1.5% (inside the 3.75% liquidation
  distance), no fixed TP, trailing stop at 1.0% from the best mark once up 1.5%.
- Time stop: flat at 60 minutes whatever the P&L.
- Max one ticket per event, max three events per week (that is the calendar).

### 3b. Liquidation cascade rider — second trigger, same code

Evidence. Cascades are mechanical: each tier of liquidations is forced flow that
pushes into the next tier ($19B liquidated over Oct 10–11 2025). Order-flow toxicity
(VPIN) predicts BTC jumps with positive serial correlation in jump size
(Kitvanitphasu et al., RIBAF 2026). Extreme funding is a documented contrarian
precondition: the crowded side is the one that gets flushed.

Signals, all free from the Hyperliquid API/WS: burst of liquidation-flagged fills on
one side; open-interest drop > X% in 5 min; funding at or near the cap; the public
liquidation heatmaps (Hyperdash, hyperperps) that map wallet-level liquidation
prices on BTC/ETH/SOL.

Rules: enter *with* the cascade after the first burst, same bracket as 3a, 20x, time
stop 30 min. Never rest a stop inside a dense cluster on the heatmap: place it on
the far side. Expect worse slippage than 3a; the book is thin exactly when you
want in.

### 3c. Volatility-conditioned intraday momentum — measure, do not trade yet

Shen et al. is a half-hour-to-half-hour effect, not a minute effect, and the gains
quoted are before Hyperliquid taker fees. Log it as a filter (only take 3a/3b tickets
when the session's realized vol is in the top tercile), not as a standalone trigger.

### 3d. What not to do

- **opt.fun 1-minute ATM options at up to 1000x.** Priced at Black-Scholes fair value
  off historical vol; you are buying a straddle leg at fair plus fees with no
  information advantage. Pure negative EV.
- **Funding-rate fade as a scalp.** The evidence is real but the horizon is hours to
  days, which is not this strategy.
- **Breakout scalping without a forced-flow trigger.** The no-edge rows above are its
  expected value.
- **Cross margin, or 40x on anything but a sub-15-minute hold.**

## 4. Kalshi 15-minute BTC contracts as the same bet without the knock-out

This repo already prices Kalshi crypto threshold markets off Deribit's implied
distribution (`predictionedge/crypto.py`, `deribit.py`). A deep out-of-the-money
15-minute or hourly YES at 5–15¢ is the same "occasional hit" payoff (7x–20x) with
**no path dependence**: a wick cannot knock it out, there is no funding, and the loss
is exactly the premium. The costs are Kalshi's fee schedule and the fact that the
crowd prices these actively.

Two uses:
1. **Cross-check.** Before an event, compare the Deribit-implied probability of the
   move you are betting on with the Kalshi price. If Kalshi's OTM contract is cheaper
   than the perp ticket on an EV basis, buy the contract instead.
2. **Hedge or straddle.** A perp ticket in one direction plus a cheap Kalshi contract
   on the other side is a crude straddle for the release moment.

Kalshi settles on the CF Benchmarks RTI, Hyperliquid marks on its own oracle; they
can diverge by tens of basis points at the print, which is itself something to log.

## 5. Sizing and bankroll policy

- Ring-fence the strategy in a Hyperliquid **sub-account** funded with money that can
  go to zero. Trade it through an **agent wallet** with no withdrawal rights.
- Ticket = 1–2% of the sub-account. Never scale a ticket on conviction.
- Max 3 tickets per day, max 1 per trigger event. Stop for the week after 6
  consecutive stop-outs (with a 30% hit rate that is a 12% event, not a 1% one, so it
  is a pause, not a verdict).
- Withdraw half of any week's gains back to the main account. The convex book's
  enemy is redeploying a hit at the same fraction.
- Kill switch: reuse `data/STOP` semantics; the bot cancels open orders and flattens.

## 6. What kills this in practice

1. **Fees.** At 40x, 5% of the stake per round trip. Get to a lower taker tier or
   stake HYPE for the discount before 40x is even discussable; at 20x it is 2.6%.
2. **Funding cap.** 4%/hour on notional is 80% of the stake per hour at 20x. Check
   the current funding before entry; skip if it is against you and above 0.5%/hour.
3. **Mark lag.** Your stop triggers on a median that includes a 150 s EMA. In a
   cascade the mark can jump past both the stop and the liquidation price in one
   3-second oracle update. Budget the stop at 1.5% but *expect* fills at 2%+.
4. **Slippage on exit.** The book is thinnest exactly when the cascade is running.
   TP/SL are market orders; IOC exits with a 3% slippage bound are the ceiling.
5. **Backstop liquidation** keeps the maintenance margin. A tight stop avoids it;
   a stop that never fills does not.
6. **Position count and rate limits** are not an issue; **WS disconnects** are. A bot
   that loses its socket during the event is the single most likely operational loss.

## 7. Validation gate before any money

1. Collect: 1-minute candles, the trades stream with liquidation flags, OI, funding,
   and the oracle/mark series for BTC and ETH, continuously, plus the macro calendar.
   The API returns 5,000 candles per call, so backfill incrementally.
2. Event backtest: replay the last 24+ months of CPI/FOMC/NFP/PCE (40+ events) with
   the 3a rules, fees at the base taker tier, slippage 2 bp, fills 1 oracle tick late.
3. Score it the right way. Deflated Sharpe (this repo's `backtest.py`) assumes roughly
   symmetric returns and will reject a strategy whose whole value is three fat wins.
   Use a bootstrap of the per-ticket return distribution and require the 5th-percentile
   total return over 40 tickets to be above −25% of bankroll at the chosen fraction,
   plus a hit rate whose lower confidence bound clears 1/(1+b).
4. Paper on **testnet** through at least three live events; then paper on mainnet
   with the smallest tradeable size; then 1% tickets.
5. Promote through the same paper-track-record gate as everything else in `PLAN.md`
   (WS-F). No exceptions for a strategy whose selling point is variance.

## 8. Relationship to the existing constraints

`EDGE_PLAN.md` §0 rules out speed edges because Omen is manual-only. That constraint
is venue-specific: Hyperliquid permits API trading and agent wallets, so this is the
one place in the project where a sub-5-minute reaction is allowed. It is also the one
place where the "advisory only" line is crossed, so it must live in its own module
with its own arm switch and its own sub-account, never sharing capital with the
Kalshi/Polymarket book.

## Sources

Hyperliquid mechanics: Hyperliquid docs (margining, margin tiers, liquidations,
funding, order types, robust price indices); Eco support articles on Hyperliquid
margin/leverage, liquidations and fees (2026); Buildix, "How Hyperliquid liquidations
actually work" (2026); Hyperliquid Guide, leverage and API guides (2026); Dwellir and
perp.wiki funding-rate explainers; Chainstack API guide; Glassnode Hyperliquid
latency dashboard; DL News on opt.fun 1000x one-minute options.

Evidence on short-horizon edge: Block Scholes, "Is Bitcoin showing greater
sensitivity to US CPI releases again?" (2026); Nazaruk, "Bitcoin's reaction to U.S.
FOMC and CPI announcements" (KSE thesis, 2026); "Exploring volatility reactions in
cryptocurrency markets using intraday macroeconomic news analysis" (ScienceDirect,
2025); Shen, Urquhart & Wang, "Bitcoin intraday time series momentum" (Financial
Review, 2022); "Intraday return predictability in the cryptocurrency markets:
momentum, reversal, or both" (NAJEF, 2022); Kitvanitphasu et al., "Bitcoin wild moves:
evidence from order flow toxicity and price jumps" (RIBAF, 2026); Inan,
"Predictability of funding rates" (SSRN, 2025); Bitcoin.com / Mudrex on the Oct 2025
liquidation cascade; VanEck Mid-August 2026 Bitcoin ChainCheck (realized vol).

Kalshi crypto contracts: Kalshi 15-minute crypto category page; The Lines / SailGP
15-minute market guides; "Do prediction markets forecast cryptocurrency volatility?
Evidence from Kalshi macro contracts" (arXiv 2604.01431).
