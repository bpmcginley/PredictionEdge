# PredictionEdge — Effectiveness & Profitability Plan

Written 2026-07-07. This is the working plan to take the bot from "infrastructure complete,
edge unproven" to "small, real, measured edge, running unattended." It is structured as
discrete workstreams so each can be handed to a subagent in a workflow later.

---

## 1. Where we actually are (honest baseline)

**Built and working (107 tests passing):**
- Kalshi client (RSA-PSS auth, orders, settlement reconciliation), demo + prod.
- Polymarket US client (Ed25519 auth, BBO reads, order placement) — *auth verified once,
  live orders placed once*.
- Whale layer: Polymarket leaderboard scoring + holder/trade feeds; copy-signal detection
  (`copytrade.py`) with min-$ notional, wallet-quality, and price filters.
- Crypto lognormal pricing model (Coinbase spot + realized vol) — **paper-only**, gated off
  live because it systematically disagrees with liquid markets (model 0.91 vs market 0.98).
- Flow/spike detector, de-vig sportsbook comparison (Odds API — free quota exhausted).
- Safety rails: ARM switch, kill-switch file, exposure/position/daily-loss caps, price bands,
  paper-vs-live status separation, SQLite state store.
- Flask dashboard (likes, whale edges, copy signals, spikes, ARM button) + /history page.

**Live results so far (the only real data we have):**
- One armed session (2026-06-26): 4 copy orders on PM-US, all on **in-play World Cup games**
  (~$14.86 stake, ~$7.65 filled). This exposed the three biggest strategy flaws (§2).

**Currently broken / blocked:**
- **PM-US auth returns 401** — key rotated/revoked after chat exposure. Live PM-US is dead
  until new `PMUS_API_KEY_ID` / secret are set in `.env`. (Kalshi keys were also exposed →
  must be rotated too if not already.)
- The 4 June-25 orders still show `open` in state.db — **no PM-US settlement reconciliation**
  exists, so realized P&L on PM-US is never recorded and the daily-loss breaker can't see it.
- Kalshi account ~$0.04 — effectively unfunded; every Kalshi "live" path is untested with
  real fills.
- Odds API: 0/500 quota; sportsbook de-vig comparisons are dark until reset or a new key.

---

## 2. What the live test taught us (drives everything below)

1. **In-play copying is anti-edge.** Whales react to goals faster than a 5-min-cycle bot;
   we fill at the post-event price and hold coin-flip variance. Filter: skip markets
   resolving < 24–48 h out, and *never* in-play.
2. **The bot doubled the same bet.** `NO usa` + `YES tur` on one game = same position twice
   (ditto par/aus). Need game/event-level dedup, not market-level.
3. **Position display ≠ order sizing.** Sizing was correct ($4/order); the scare was
   accounting. Keep, but add per-EVENT stake caps so correlated legs can't stack.

---

## 3. Workstreams (each = one subagent-sized unit)

### WS-A — Restore live capability (prereq for everything live)
**Goal:** both venues authenticated, funded, and testable again.
- [ ] User: rotate + supply fresh PM-US keys (and Kalshi if not rotated). Bot: verify with
      read-only balance calls. *(User action; agent verifies.)*
- [ ] Add a `preflight` dashboard tile: per-venue auth OK / balance / last error, so a dead
      key is visible in seconds, not discovered at order time.
- **Files:** `.env`, `polymarket_us.py`, `dashboard.py/html`.
- **Done when:** both balances render on dashboard with no manual steps.

### WS-B — PM-US settlement reconciliation + true P&L
**Goal:** the bot knows what actually happened to its money. Without this, no strategy can
be evaluated and the daily-loss breaker is blind on PM-US.
- [ ] Poll PM-US positions/fills each cycle; mark state.db orders `settled_won/lost/cancelled`;
      write realized P&L into the `pnl` table (feeds the existing breaker).
- [ ] Backfill the 4 stuck June-25 orders.
- [ ] Dashboard: realized P&L (day / all-time) per venue; /history shows outcomes.
- **Files:** `polymarket_us.py` (positions/fills endpoints), `runner.py` (settle step),
  `state.py`, `history.html`.
- **Done when:** /history shows the June-25 batch resolved with correct realized P&L.

### WS-C — Copy-trade filter hardening (the actual edge repair)
**Goal:** only copy where whale-following plausibly has edge.
- [ ] **Time filter:** skip markets resolving < `copytrade_min_hours_to_resolve` (default 24 h);
      hard-skip in-play sports (game start time ≤ now).
- [ ] **Event-level dedup + cap:** one position per event (game), correlated-leg detection
      (YES teamA ≈ NO teamB); per-event stake cap.
- [ ] **Staleness filter:** skip if whale's fill is > N minutes old or price moved > X¢ since
      their fill (we'd be buying their exhaust).
- [ ] **Wallet quality v2:** per-wallet trailing win-rate/ROI by category (a politics whale
      is not a soccer whale); require category-matched competence.
- **Files:** `copytrade.py`, `config.py`, `runner.py`, tests.
- **Done when:** replaying the June-26 signals through the new filters produces **0 orders**
  (all four were in-play), and slower-market signals still pass.

### WS-D — Kalshi-native edge (make the *Kalshi* bot earn its name)
**Goal:** a validated, liquid Kalshi strategy — currently we have none live.
Candidates in priority order:
1. **Kalshi ↔ PM price-gap scanner:** same event priced on both venues; act on gaps beyond
   fees. Structural, doesn't need a forecast to be right. Needs the market-matching layer
   (title/entity matcher — slugs never match across venues).
2. **Sportsbook de-vig vs Kalshi sports** (existing code) — blocked on Odds API quota;
   revive when quota resets, add hard monthly call budget + cache (already 30-min cached).
3. **Crypto daily series (KXBTCD etc.):** stays paper until WS-E validates the model.
- [ ] Build the cross-venue matcher (shared entity/date extraction, fuzzy title match, manual
      alias table for recurring series).
- [ ] Paper-trade the gap scanner ≥ 2 weeks; promote to live only if paper P&L after fees > 0
      with ≥ 20 samples.
- **Files:** new `matching.py`, `arbscan.py`; `runner.py` wiring; tests.
- **Done when:** dashboard shows live gap candidates with fee-adjusted edge, paper track
  record accumulating.

### WS-E — Crypto model validation loop (already requested, not yet built)
**Goal:** decide with data whether the lognormal model ever goes live.
- [ ] Nightly job: record model prob vs market prob vs *actual outcome* for every daily
      crypto market; compute calibration (Brier score model vs market-as-forecaster).
- [ ] Auto-verdict: model must beat the market's own Brier over ≥ 100 resolved markets to
      unlock `crypto_live`. Until then it stays paper (current default).
- **Files:** `crypto.py`, new `calibration.py`, `state.py` (outcomes table), dashboard tile.
- **Done when:** dashboard shows "model vs market Brier: X vs Y over N markets" and the gate
  is mechanical, not vibes.

### WS-F — Paper-track-record engine (promotion gate for every strategy)
**Goal:** one uniform rule: *nothing trades live until its paper record earns it.*
- [ ] Tag every paper order with `strategy` (copy / gap / crypto / devig); nightly rollup of
      per-strategy paper P&L, hit-rate, sample count.
- [ ] Promotion rule in config: live only when samples ≥ N and P&L after modeled fees > 0.
      Demotion rule: live strategy that draws down > X reverts to paper automatically.
- **Files:** `state.py` (strategy column), `runner.py`, dashboard "strategy report card".
- **Done when:** the ARM button arms only strategies that have passed their gate.

### WS-G — Unattended operation hardening
**Goal:** it genuinely runs itself.
- [ ] Windows Scheduled Task (or NSSM service) for the dashboard+engine; auto-restart on crash;
      heartbeat stamped to state.db meta shown on dashboard ("engine alive 2 min ago").
- [ ] Error budget: 3 consecutive cycle exceptions → auto-disarm + STOP file + visible banner.
- [ ] Log rotation; startup self-test (auth, DB, clock skew) before first cycle.
- **Files:** new `service.md` runbook, `runner.py`, `dashboard.py`.
- **Done when:** machine reboot → bot resumes, disarmed-by-default, dashboard reachable.

### WS-H — Capital & risk policy (small money, survive to learn)
**Goal:** caps that match reality: ~$20 PM-US, Kalshi pending funding.
- [ ] Per-strategy exposure sub-caps (e.g. copy ≤ $10, gap ≤ $10) instead of one global $200
      ceiling that dwarfs the bankroll.
- [ ] Kelly fraction sanity: with a $20 bankroll, min order sizes dominate — fixed $2–4
      stakes are correct; document that Kelly activates only above ~$200 bankroll.
- [ ] Weekly loss breaker in addition to daily.
- **Files:** `config.py`, `risk.py`/`runner.py`, tests.

---

## 4. Sequencing & dependencies

### WS-I — Kalshi Liquidity Incentive Program maker bot (NEW 2026-07-07, time-sensitive)
**Goal:** earn LIP daily reward pools by resting two-sided quotes in quiet markets.
Wired 2026-07 (Sonnet pass): `maker.py`'s `KalshiMakerVenue` + `MakerEngine.cycle()` are
real, `kalshi.py` has `orderbook()`/`resting_orders()`, and `maker_runner.py` is a
standalone entry point (`python -m predictionedge.maker_runner [--daemon] [--mock]`),
covered by `tests/test_maker.py` (16 tests, all pure-logic + `MockMakerVenue` orchestration,
no network). Research facts baked into the module docstring:
- Program pays through **Sept 1, 2026** — every week of delay is forgone rewards. Open to
  regular members, no MM agreement; $10–$1,000/day pools per market; size targets start
  at 100 contracts (our tier: ~100 contracts in ≤35¢ markets).
- Maker fee is now **25% of taker fee** (not free); `maker.spread_clears_fees` gates on it
  by calling `fees.fee_per_contract(mid, maker=True)` directly (no duplicate fee math).
- Scoring = second-by-second uptime × size × proximity to top-of-book → requote discipline
  matters (`diff_quotes` only moves past a threshold).
- Adverse selection is the loss mode: quote only slow series (weather highs, econ between
  prints), pull quotes around scheduled releases (`should_pull` + a release calendar).
- [x] `MakerVenue` methods on `LiveKalshiTradingClient` (orderbook, resting orders) + a
      manually-vetted-allowlist `lip_markets()` (no official eligibility API exists).
- [x] `MakerEngine.cycle()` — select markets, diff quotes, throttle requotes, pull on
      staleness, execute (or log, if `dry_run`) via the venue.
- [x] `maker_runner.py` entry point, sharing `RiskManager.preflight()` and
      `control.armed()` with the main bot (one arm switch, one kill-switch for both).
- [ ] Release calendar: `MakerEngine.event_window()` is a placeholder that always returns
      `None` — unsafe to run live on news-sensitive series until a real per-series
      calendar (NWS, BLS/CPI) is wired in.
- [ ] Fills are NOT yet persisted: `StateStore` has no `strategy` column and
      `MakerEngine.cycle()` never calls `state.record_order()` — this is WS-F's job
      (tag maker fills `strategy="maker"` so the paper-track-record gate can see them).
- [ ] Request the free rate-limit upgrade (basic 200r/100w → 300/300, one API call).
- [ ] Dry-run ≥ 3 days (log intended quotes vs realized book) before first live quote —
      blocked on WS-A (rotated Kalshi keys) and picking + vetting an actual series for
      `PE_MAKER_SERIES` (empty by default, on purpose).
- **Files:** `maker.py`, `maker_runner.py`, `kalshi.py`, `tests/test_maker.py`,
  `.env.example` (PE_MAKER_* block). Not yet touched: `state.py` (strategy column),
  a release-calendar module, `risk.py`/`runner.py` integration beyond preflight+armed.
- **Done when:** dashboard shows LIP rewards accrued + maker fills P&L, bot quotes 2–3
  allowlisted markets unattended with pull-windows honored.
- **Expected:** $10–60/mo at our size; honest range includes negative months if adverse
  selection is mishandled — the paper gate applies here like everywhere else.

### WS-D+ — Cross-venue gap scanner framework (laid 2026-07-07)
`arbscan.py` created: gap math + gating implemented (fee-adjusted net edge, capital-lock
annualized-return filter, `MANUAL_VERIFIED` pair gating so fuzzy matches can never trade).
Remaining wire-up: cross-venue matcher (extend `matching.py` beyond sports), `arb_pairs`
state table, dashboard verify-pair tile, BBO fetch + paper legs via `executor.py`.
Key design decision: resolution-rule mismatch is treated as THE risk — a human must
verify both venues' rule texts before a pair is tradable, one click on the dashboard.

```
WS-A (keys) ──► WS-B (settlement/P&L) ──► WS-F (track-record engine)
                     │                         │
WS-C (copy filters) ─┴──► live copy v2 (gated by WS-F)
WS-D (matcher + gap scan, paper) ────────► live gap (gated by WS-F)
WS-E (crypto calibration, paper) ────────► crypto live (gated by WS-E verdict)
WS-G (unattended)  — parallel, any time after WS-A
WS-H (risk policy) — parallel, small, do early
```

Recommended order for a workflow run: **A → B → C+H (parallel) → F → I → D → E → G.**
WS-I jumps the queue among strategies because the LIP program sunsets Sept 1, 2026, and it
needs only WS-A (Kalshi keys + funding the account — LIP quoting needs real collateral).
A is blocked on the user supplying rotated keys; everything else in B–H can be built and
tested with mocks first.

## 5. What "profitable" means here (success criteria)

- Realized (not paper) P&L ≥ $0 after fees over a rolling 30 days, with every live order
  attributable to a strategy that passed its paper gate.
- Zero safety incidents: no order over cap, no trade while disarmed, no stuck-open orders
  older than resolution date.
- Honest accounting: dashboard realized-P&L matches venue statements to the cent.

## 6. Standing risks / non-goals

- **Bankroll is ~$20.** This phase is about *proving process*, not income. Expect noise to
  dominate results until sample counts grow.
- PM-US market set ≠ international Polymarket — many whale signals are structurally
  uncopyable in the US. Accept it; don't chase workarounds (VPN etc. = ToS violation).
- No in-play sports, ever, at 5-min cadence. No long-dated markets (user preference).
- Exposed keys (chat) must be treated as compromised until rotated — WS-A gate.
