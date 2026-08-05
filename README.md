# PredictionEdge

An autonomous, fee-aware trading bot for **Kalshi** prediction markets that runs
unattended on a schedule — no Claude, no app, no human in the loop at runtime.

**Primary edge:** fade Kalshi prices against a de-vigged sharp **sportsbook
consensus** (treat the sharp books as true probability; trade Kalshi only when it
diverges past the fee + slippage + margin threshold).

**Supporting signal:** smart-money / "whale" flow from public **Polymarket
on-chain** data (interface in place, weight 0 until validated out-of-sample).

**Risk mitigation:** an order-review step revisits resting orders every cycle and,
when the odds have moved, either cancels a stale (pickable-off) maker order or
places a closed-form **arbitrage hedge** on the inverse side (`arbitrage.py`).

## Safety model (read this before going live)

The bot handles real money while you're not watching, so safety is layered:

- **Dry-run by default.** Real orders are placed *only* when `PE_LIVE_TRADING=1`.
  Without it, every "trade" is recorded to the paper ledger and state DB.
- **Demo-first.** Even with live trading on, it defaults to Kalshi's **demo**
  environment (`PE_USE_DEMO=1`). Production is **blocked** until a clean demo run
  is recorded (`require_demo_first`).
- **Circuit breakers** (all hard stops, in `risk.py`): daily-loss limit, total
  exposure cap, max open positions, max orders/cycle, a price band, and a
  "too-good-to-be-true" edge ceiling that refuses to fire size into a data glitch.
- **Arm switch.** Live orders require the dashboard's ARM button (a runtime flag the
  runner reads each cycle) *in addition to* the `PE_LIVE_TRADING` capability — so a
  running bot is two independent switches away from real orders, and the button
  defaults OFF.
- **Kill switch.** Create the file `data/STOP` and all trading halts immediately
  (overrides the arm switch); delete it to resume. No restart needed.
- **Idempotent + durable.** Every order is persisted (SQLite) before the API call,
  so a crash or restart never double-fills or forgets a position.
- **Heartbeat.** Each cycle writes `data/heartbeat.json` so you can see it's alive.

## Quick start

```bash
# Sleek local dashboard — what it likes, what it would do, live whale spikes:
python -m predictionedge.dashboard            # then open http://127.0.0.1:8787

# Full autonomous pipeline on canned data — no credentials, no orders:
python -m predictionedge.run --mock           # one cycle
python -m predictionedge.run --mock --status  # heartbeat + ledger + exposure

# Read-only opportunity printer (human view):
python -m predictionedge.main --mock

# Tests (stdlib only; no pytest needed):
python -m pytest -q   # or the stdlib harness in CONTRIBUTING notes below
```

## Operating runbook

Each step has a human gate (your keys, real calendar time, your go). The code makes
every gate concrete.

0. **Setup** — `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` (venv + deps).
1. **Add keys** — `cp .env.example .env`; fill `KALSHI_API_KEY_ID`,
   `KALSHI_PRIVATE_KEY_PATH`, `ODDS_API_KEY`. Leave `PE_LIVE_TRADING=0`.
   Then `python -m predictionedge.doctor` must report **READY** (checks creds, key
   loads, kill-switch, Kalshi + Odds reachability).
2. **Collect (dry-run)** — schedule it:
   `powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -IntervalMinutes 30`.
   It scans/settles real markets and writes labelled bets to `data/settled_bets.jsonl`
   while placing **no** orders. Let it run for weeks.
3. **Grade the edge** — `python -m predictionedge.backtest --oos`. Must report
   `deploy: true` (Deflated Sharpe + bootstrap CI, in-sample **and** out-of-sample)
   before any caps go up.
4. **Whales (optional)** — build the map from slugs:
   `python -m predictionedge.gamma --build pairs.json data/whale_map.json`, set
   `PE_WHALE_SOURCE=polymarket`, and only lift `PE_WHALE_WEIGHT` once the signal
   validates out of sample.
5. **Demo** — `PE_LIVE_TRADING=1`, `PE_USE_DEMO=1`. A clean demo cycle unlocks prod.
6. **Production, tiny** — `PE_USE_DEMO=0` with small caps. Watch `data/heartbeat.json`
   and the log; `data/STOP` halts everything instantly.

### Scheduling (runs without Claude)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -IntervalMinutes 30
```

Registers a Windows Scheduled Task that fires `scripts\run_bot.ps1` every 30 min,
whether or not anything else is open. Remove with
`Unregister-ScheduledTask -TaskName PredictionEdge -Confirm:$false`.

## Architecture

```
sportsbook odds ─► de-vig ─► consensus fair prob ─┐
 Polymarket whale flow ─► signal ─► blend ◄────────┘
                                       │
   Kalshi data ─► find_edge (net of fees) ─► fractional-Kelly sizing under caps
                                       │
   ┌── every cycle (runner) ───────────┴───────────────────────────────────┐
   │ 1. review open orders: stale-cancel / arbitrage-hedge (arbitrage.py)   │
   │ 2. preflight risk gate  3. scan  4. per-order risk vet  5. execute     │
   └── dry-run executor (default) │ live executor (gated) ─► Kalshi orders ──┘
                                  ▼
                state (SQLite) + paper ledger + heartbeat
```

| Module | Role |
|---|---|
| `fees.py` | Kalshi fee formula (taker/maker, per-contract for EV) |
| `devig.py` | odds → fair probabilities (3 methods) |
| `odds.py` | sportsbook ingest + consensus (mock + The Odds API) |
| `kalshi.py` | data client + **live trading client** (orders/cancel/balance), verified contract |
| `polymarket.py` | Polymarket public data client (leaderboard + market holders) |
| `whales.py` | smart-money wallet selection + signal provider + blend |
| `normalize.py` | team-name normalisation for matching |
| `matching.py` | **auto cross-venue matching** (Kalshi ↔ sportsbook) + audited links |
| `edge.py` | positive-EV detection + Kelly sizing |
| `whale_edge.py` | whale-as-primary-signal edge for politics/econ/news markets |
| `arbitrage.py` | closed-form hedge sizing (the PDF spec) |
| `scanner.py` | shared scan → ranked opportunities |
| `risk.py` | circuit breakers: preflight + per-order vet + hedge vet |
| `state.py` | durable SQLite: orders, daily P&L, meta |
| `executor.py` | DryRun (default) and Live order executors |
| `review.py` | revisit resting orders when odds move (cancel / hedge) |
| `discovery.py` | live Kalshi sports discovery → matched links |
| `settle.py` | settle resolved markets → realised P&L + labelled bets |
| `backtest.py` | Deflated-Sharpe + bootstrap deploy gate over settled bets |
| `runner.py` | the autonomous cycle (settle → review → scan → execute) + heartbeat |
| `run.py` | scheduler entry point (`--once` default, `--daemon`, `--status`) |
| `doctor.py` | preflight readiness check (creds, key, connectivity, posture) |
| `gamma.py` | Polymarket slug → condition_id resolver / whale-map builder |
| `dashboard.py` + `dashboard.html` | sleek local web dashboard (likes / intents / whale spikes) |
| `flow.py` | whale-flow spike detector — sudden large bets on event markets |
| `divergence.py` | Polymarket-vs-Kalshi price divergence report (CLI) |
| `control.py` | runtime arm switch (dashboard ON/OFF, read by the runner each cycle) |
| `ledger.py` | append-only paper-trade ledger |
| `main.py` | read-only human-facing opportunity printer |

## Whale-following (smart-money signal)

Polymarket's public Data API exposes every wallet's trades and positions.
`polymarket.py` reads the leaderboard (`/v1/leaderboard`, ranked by PnL) + per-market
holders (`/holders?market=<condition_id>`); `whales.py` selects "smart money" wallets
(PnL + ROI floors that exclude churny market-makers; sample-size/win-rate floors apply
when known), then turns their positioning on a matched market into a probability +
confidence that nudges fair value. Enable with `PE_WHALE_SOURCE=polymarket` and a
`data/whale_map.json` of `{ "KALSHI_TICKER": "polymarket_condition_id" }` (0x+64 hex,
resolve a slug via gamma-api). `PE_WHALE_WEIGHT` stays 0 until validated.

**Two strands, by market type.** Sports get the de-vig sportsbook fade — sportsbooks
are sharp and games aren't insider-driven, so following whales there adds little.
**Politics / economics / news** are the opposite: no sportsbook to de-vig, and exactly
where informed/insider money has an edge. For those, `whale_edge.py` makes a confident
smart-money signal the *fair value itself* (`PE_WHALE_PRIMARY`,
`PE_WHALE_PRIMARY_MIN_CONF`) and fades Kalshi when it diverges. Put politics/econ
markets — not games — in `whale_map.json`.

> **The whale signal is a soft nudge, never a hedge.** Kalshi (CFTC source agencies)
> and Polymarket (UMA oracle) can settle the *same* worded market **oppositely** - a
> documented Feb 2026 Super Bowl market did exactly that. So a "transferred" signal
> informs fair value; it is never treated as a riskless cross-venue hedge.

## Whale trade spikes (insider-flow detector)

`flow.py` polls Polymarket's public large-trade feed (`/trades`, ≥ $25k) and surfaces
sudden big bets on **event** markets — politics, geopolitics, world events — where a
large position is most likely informed (the canonical case: a massive bet on a
"will X happen" market right before it happens). Crypto micro-scalping and routine
sports/esports games are filtered out. The dashboard shows each spike with size,
direction, trader, time, and time-to-close (so near-resolution bets stand out). Run
`python -m predictionedge.divergence` to see where Polymarket and Kalshi disagree on
mapped markets — the candidates worth trading.

## Cross-venue matching

`matching.py` auto-pairs a Kalshi market to its sportsbook event: team names are
normalised (city/nickname/abbreviation via `normalize.py`) and **both** teams must
agree, within a kickoff-time window - a deliberately high bar, since a wrong match
turns edge into noise. Markets are parsed from Kalshi's authoritative
`yes_sub_title`/`no_sub_title` fields (not the ticker, per Kalshi's own guidance),
which also orient which team YES pays out on. Enable live discovery with
`PE_DYNAMIC_DISCOVERY=1` over the verified moneyline series
(`KXNBAGAME/KXNFLGAME/KXMLBGAME/KXNHLGAME/KXEPLGAME`).

## Backtest / validation gate

`backtest.py` answers the only question that matters before scaling caps: does the
edge survive fees out of sample? Each settled market is booked by `settle.py` into a
labelled dataset, and `python -m predictionedge.backtest` scores it with the same
rigor as the StockTradingLol optimizer - a per-bet **Deflated Sharpe** (punishing
multiple-testing via `--trials`), a **block-bootstrap** CI on mean return, and
calibration (Brier) - behind a single deploy gate that must pass on all counts.

## Roadmap

1. **(done)** Read-only plumbing, de-vig edge core, fees, Kelly sizing, tests.
2. **(done)** Autonomous runner, durable state, risk/circuit-breaker layer,
   arbitrage order-review, Windows scheduling. Runs unattended in dry-run today.
3. **(done)** Verified Kalshi trading API contract wired into `kalshi.py` (auth,
   order fields, cents/dollars dual-schema parsing, v2 cancel path). Confirm on demo.
4. **(done)** Auto cross-venue matching + Polymarket smart-money signal, both wired to
   verified endpoints (Data API leaderboard/holders; Kalshi `*GAME` series + sub-title
   parsing) and tested.
5. **(done)** Settlement loop + backtest/validation harness (Deflated Sharpe,
   block-bootstrap, deploy gate) that labels real outcomes and grades the edge.
6. **Next:** run scheduled in dry-run to accumulate a real settled-bets dataset; pass
   the deploy gate before any caps go up; validate the whale signal out-of-sample
   before lifting `whale_weight`; curate `whale_map.json` condition-ids by hand
   (false-match cost is high). Then gated, tiny live execution on demo → prod.

## Legal / risk notes

- Kalshi only for execution. International Polymarket is geoblocked for US persons —
  read it for signal, never trade it from a US IP.
- US state law is tightening (some states block markets; Minnesota criminalized
  *operating* a prediction market effective Aug 1 2026). Check your state.
- Kalshi issues tax forms and enforces position limits. Edges are thin and fees are
  real — keep the caps small and let the paper ledger earn your trust first.
