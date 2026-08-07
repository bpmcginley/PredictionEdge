"""Risk-neutral threshold probabilities from Deribit's BTC/ETH option chain.

`crypto.py` priced threshold markets ("BTC above $70k on Friday") from *realized*
volatility under a zero-drift lognormal. That is the wrong object: realized vol is a
backward-looking property of the price path, while a threshold market pays off on the
market's *forward-looking risk-neutral* distribution. The two differ by the whole
variance risk premium and the entire skew, which is why the old model read ~0.91 where
liquid markets read ~0.98. A 7c disagreement with a liquid market is model error.

Deribit is the deepest crypto option venue that exists and publishes its full chain
free and unauthenticated. From the chain we read the digital directly:

    P(S_T > K) = -dC/dK

the negative slope of the call curve. That is exactly what a threshold market pays, and
it is more robust than a full Breeden-Litzenberger density reconstruction, which needs a
*second* derivative of noisy quotes to say anything.

VERIFIED LIVE 2026-08-06 (do not trust remembered API shapes; these were checked):

  GET {base}/public/get_book_summary_by_currency?currency=BTC&kind=option
    -> {"result": [ {instrument_name, bid_price, ask_price, mark_price, mark_iv,
                     underlying_price, open_interest, ...}, ... ]}
    One call returns the whole chain (834 BTC instruments). bid_price/ask_price are
    None when that side is unquoted.

  GET {base}/public/get_instruments?currency=BTC&kind=option&expired=false
    -> {"result": [ {instrument_name, strike, option_type, expiration_timestamp (ms),
                     ...}, ... ]}
    instrument_name is "BTC-8AUG26-64000-C", so strike and type parse from the name,
    but the exact expiry instant does not - hence the second call. This list is also
    the only way to know which strikes Deribit *lists* but nobody quotes, which is
    what the hole check below needs.

  GET {base}/public/get_index_price?index_name=btc_usd
    -> {"result": {"index_price": ..., "estimated_delivery_price": ...}}

  Prices are quoted in COIN units (BTC per contract), not USD. The USD value is
  price_coin * underlying_price. Verified: for BTC-21AUG26 calls, mark_price *
  underlying_price reproduced undiscounted Black-76 with F=underlying_price, r=0 and
  sigma=mark_iv to a relative 4e-5. So `underlying_price` is the FORWARD for that
  expiry (64554 on 28AUG26 against a 64372 index - the basis is real and must not be
  ignored), and the coin-quoted premium times that forward is the undiscounted USD
  call value. Deribit's `interest_rate` field is 0, so there is no discount factor to
  strip and the digital is read straight off the slope.

CAVEAT worth stating out loud: Deribit settles on its own BTC/ETH index and Kalshi
settles on a different one. They track each other closely but not exactly, and this
module does not model that basis. It is small relative to the 7c error it replaces.

Everything here is read-only public market data. No key, no order path.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import NormalDist

from .config import Config

_ND = NormalDist()
_YEAR_SECONDS = 365.25 * 24 * 3600

# "BTC-8AUG26-64000-C" -> asset, expiry code, strike, C|P.
_NAME = re.compile(r"^([A-Z]+)-([0-9]{1,2}[A-Z]{3}[0-9]{2})-([0-9.]+)-([CP])$")

# An implied vol outside this band is not a quote, it is a data error. Live BTC marks
# sit roughly 25%-90%; the band is deliberately loose enough to catch only nonsense.
_IV_FLOOR, _IV_CEIL = 0.02, 5.0

# A digital outside [0,1] means the smile we built is not arbitrage-free. Tiny
# excursions are rounding; anything bigger is a broken chain and must not be reported.
_DIGITAL_TOL = 0.02

_INDEX_NAME = {"BTC": "btc_usd", "ETH": "eth_usd"}


# --------------------------------------------------------------------------- fetch


@dataclass(frozen=True)
class OptionQuote:
    """One genuinely two-sided option quote, already converted to USD."""

    instrument: str
    expiry_ts: float        # seconds since epoch, UTC
    strike: float
    is_call: bool
    forward: float          # Deribit's `underlying_price` = forward for this expiry
    bid_usd: float
    ask_usd: float

    @property
    def mid_usd(self) -> float:
        return (self.bid_usd + self.ask_usd) / 2.0

    @property
    def spread_frac(self) -> float:
        m = self.mid_usd
        return (self.ask_usd - self.bid_usd) / m if m > 0 else float("inf")


@dataclass
class Chain:
    """A whole asset's option chain: the quotes, and every strike Deribit lists.

    `listed` is not derivable from `quotes` and is the entire point of keeping it: a
    strike that Deribit lists but nobody quotes is a HOLE in the ladder, and holes are
    what this module refuses to interpolate across.
    """

    asset: str
    quotes: list[OptionQuote] = field(default_factory=list)
    listed: dict[float, tuple[float, ...]] = field(default_factory=dict)
    index: float = 0.0


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def parse_chain(asset: str, summaries: list[dict], instruments: list[dict],
                index: float = 0.0) -> Chain:
    """Join the book summary to the instrument list into two-sided USD quotes.

    Rejects anything without a genuine two-sided book. A one-sided or crossed quote is
    not a price, it is the absence of one, and the whole point of this module is to stop
    inventing numbers the data does not support.
    """
    chain = Chain(asset=asset.upper(), index=index)
    # The instrument list is authoritative for both the expiry instant and the strike;
    # the name is only a fallback. Deriving a quote's strike from its name and the
    # ladder's strikes from this field would let the two disagree by a rounding, and
    # the hole check compares them.
    meta: dict[str, tuple[float, float]] = {}
    listed: dict[float, set[float]] = {}
    for i in instruments:
        name = i.get("instrument_name") or ""
        m = _NAME.match(name)
        exp_ms = _f(i.get("expiration_timestamp"))
        strike = _f(i.get("strike")) or (_f(m.group(3)) if m else None)
        if not m or exp_ms is None or not strike:
            continue
        ts = exp_ms / 1000.0
        meta[name] = (ts, strike)
        listed.setdefault(ts, set()).add(strike)
    chain.listed = {ts: tuple(sorted(ks)) for ts, ks in listed.items()}

    for s in summaries:
        name = s.get("instrument_name") or ""
        m = _NAME.match(name)
        hit = meta.get(name)
        if not m or hit is None:
            continue
        ts, strike = hit
        fwd = _f(s.get("underlying_price"))
        bid, ask = _f(s.get("bid_price")), _f(s.get("ask_price"))
        if not strike or not fwd or fwd <= 0:
            continue
        if bid is None or ask is None or bid <= 0 or ask <= bid:
            continue    # unquoted, zero-bid or crossed -> no price here
        chain.quotes.append(OptionQuote(
            instrument=name, expiry_ts=ts, strike=strike, is_call=m.group(4) == "C",
            forward=fwd, bid_usd=bid * fwd, ask_usd=ask * fwd,
        ))
    return chain


def fetch_chain(cfg: Config, asset: str, fetch=None) -> Chain:
    """Whole live option chain for one asset. Public GETs, no auth."""
    getter = fetch or _requests_get
    base = cfg.deribit_base_url.rstrip("/")
    asset = asset.upper()
    summaries = getter(f"{base}/public/get_book_summary_by_currency",
                       {"currency": asset, "kind": "option"})
    instruments = getter(f"{base}/public/get_instruments",
                         {"currency": asset, "kind": "option", "expired": "false"})
    index = 0.0
    if asset in _INDEX_NAME:
        try:
            idx = getter(f"{base}/public/get_index_price",
                         {"index_name": _INDEX_NAME[asset]})
            index = _f((idx.get("result") or {}).get("index_price")) or 0.0
        except Exception:  # noqa: BLE001 - the index is a display nicety, not the model
            index = 0.0
    return parse_chain(asset, summaries.get("result") or [],
                       instruments.get("result") or [], index)


def _requests_get(url: str, params: dict) -> dict:
    import requests
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


# ----------------------------------------------------------------------- black-76


def black76(forward: float, strike: float, sigma: float, tau: float,
            is_call: bool = True) -> float:
    """Undiscounted Black-76 option value in USD.

    Undiscounted because Deribit reports a zero interest rate and its coin-quoted
    premium times the expiry's forward reproduces exactly this (verified live). Adding
    a discount factor here would double-count the carry already inside the forward.
    """
    if tau <= 0 or sigma <= 0:
        intrinsic = forward - strike if is_call else strike - forward
        return max(0.0, intrinsic)
    sqt = sigma * math.sqrt(tau)
    d1 = (math.log(forward / strike) + 0.5 * sqt * sqt) / sqt
    d2 = d1 - sqt
    if is_call:
        return forward * _ND.cdf(d1) - strike * _ND.cdf(d2)
    return strike * _ND.cdf(-d2) - forward * _ND.cdf(-d1)


def implied_vol(price: float, forward: float, strike: float, tau: float,
                is_call: bool = True) -> float | None:
    """Invert Black-76 by bisection. None when the price is outside no-arb bounds.

    Bisection rather than Newton: vega collapses on far wings and Newton wanders off
    exactly where the quotes are already worst. Robustness beats the iteration count -
    this runs a few hundred times per scan, not a few million.
    """
    if tau <= 0 or forward <= 0 or strike <= 0 or price <= 0:
        return None
    intrinsic = max(0.0, (forward - strike) if is_call else (strike - forward))
    ceiling = forward if is_call else strike
    if not (intrinsic < price < ceiling):
        return None
    lo, hi = 1e-4, 8.0
    if black76(forward, strike, hi, tau, is_call) < price:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if black76(forward, strike, mid, tau, is_call) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# -------------------------------------------------------------------------- smile


@dataclass(frozen=True)
class Smile:
    """One expiry's total-variance curve in log-moneyness, plus its forward.

    Stored as total variance w = sigma^2 * tau against k = ln(K/F) rather than as vols
    against strikes, because that is the representation the time interpolation needs
    and the one in which the curve is closest to a straight line.
    """

    expiry_ts: float
    tau: float                      # years from the `now` it was built with
    forward: float
    k_nodes: tuple[float, ...]      # ascending log-moneyness
    w_nodes: tuple[float, ...]      # total variance at each node
    n_listed: int = 0               # strikes Deribit lists for this expiry

    @property
    def expiry_iso(self) -> str:
        return datetime.fromtimestamp(self.expiry_ts, timezone.utc).isoformat()

    @property
    def strikes(self) -> tuple[float, ...]:
        return tuple(self.forward * math.exp(k) for k in self.k_nodes)

    def _w(self, k: float) -> float:
        """Total variance at log-moneyness k, linearly interpolated between nodes.

        Callers must have already checked k is inside the node range: this clamps, and
        a silent clamp on the wings is exactly the kind of invented number that fails
        closed elsewhere in this module.
        """
        ks, ws = self.k_nodes, self.w_nodes
        if k <= ks[0]:
            return ws[0]
        if k >= ks[-1]:
            return ws[-1]
        for i in range(1, len(ks)):
            if k <= ks[i]:
                span = ks[i] - ks[i - 1]
                t = (k - ks[i - 1]) / span if span > 0 else 0.0
                return ws[i - 1] + t * (ws[i] - ws[i - 1])
        return ws[-1]

    def _step(self) -> float:
        """Differencing offset in k: half the median node gap.

        Half a gap keeps both evaluation points inside the segments adjacent to the
        node being differenced, so the slope comes out as the average of the two
        neighbouring secants - stable, and exact for a quadratic on a uniform grid.
        """
        gaps = sorted(self.k_nodes[i] - self.k_nodes[i - 1]
                      for i in range(1, len(self.k_nodes)))
        med = gaps[len(gaps) // 2] if gaps else 0.0
        return 0.5 * med

    def digital(self, strike: float) -> float | None:
        """P(S_T > strike) under the risk-neutral measure, or None if unsupported.

        Analytic rather than a finite difference of the reconstructed call curve: call
        prices here are thousands of dollars and the slope we want is O(1), so
        differencing prices throws away most of the precision. Splitting it into the
        pure-lognormal term and the skew term also makes the skew contribution visible,
        which is the whole reason this beats a flat-vol model:

            -dC/dK = N(d2) - vega * dsigma/dK
        """
        if strike <= 0 or self.forward <= 0 or self.tau <= 0 or len(self.k_nodes) < 2:
            return None
        k = math.log(strike / self.forward)
        h = self._step()
        if h <= 0 or not (self.k_nodes[0] + h <= k <= self.k_nodes[-1] - h):
            return None     # outside the quoted ladder, or no room to take a slope
        w = self._w(k)
        if w <= 0:
            return None
        sigma = math.sqrt(w / self.tau)
        dw_dk = (self._w(k + h) - self._w(k - h)) / (2.0 * h)
        dsigma_dk = dw_dk / (2.0 * math.sqrt(w * self.tau))
        dsigma_dK = dsigma_dk / strike
        sqt = sigma * math.sqrt(self.tau)
        d1 = (-k + 0.5 * w) / sqt
        d2 = d1 - sqt
        vega = self.forward * _ND.pdf(d1) * math.sqrt(self.tau)
        p = _ND.cdf(d2) - vega * dsigma_dK
        if not (-_DIGITAL_TOL <= p <= 1.0 + _DIGITAL_TOL):
            return None     # smile arbitrage: refuse rather than clamp garbage
        return min(1.0, max(0.0, p))


def _build_smile(expiry_ts: float, quotes: list[OptionQuote],
                 listed: tuple[float, ...], cfg: Config,
                 now_ts: float) -> tuple[Smile | None, str]:
    """One expiry's smile, or (None, reason). Every rejection path is a fail-closed."""
    tau = (expiry_ts - now_ts) / _YEAR_SECONDS
    if tau <= 0:
        return None, "expired"
    by_strike: dict[float, list[OptionQuote]] = {}
    forward = 0.0
    for q in quotes:
        by_strike.setdefault(q.strike, []).append(q)
        forward = q.forward
    if forward <= 0:
        return None, "no forward"

    usable: list[tuple[float, float]] = []      # (strike, total variance)
    for strike in sorted(by_strike):
        # OTM side only. An ITM quote's mid is dominated by intrinsic, so a
        # relative-spread test on it is meaninglessly lenient: live BTC-8AUG26-63000-C
        # showed a 0.13 spread fraction while its bid/ask straddled more than the
        # option's entire time value. The OTM side is also where the liquidity is.
        want_call = strike >= forward
        cand = [q for q in by_strike[strike] if q.is_call == want_call]
        if not cand:
            continue
        q = cand[0]
        if q.spread_frac > cfg.deribit_max_spread_frac:
            continue
        iv = implied_vol(q.mid_usd, forward, strike, tau, q.is_call)
        if iv is None or not (_IV_FLOOR <= iv <= _IV_CEIL):
            continue
        usable.append((strike, iv * iv * tau))

    if len(usable) < cfg.deribit_min_strikes:
        return None, f"thin: {len(usable)} usable strikes < {cfg.deribit_min_strikes}"
    lo, hi = usable[0][0], usable[-1][0]
    holes = [k for k in listed if lo < k < hi and all(k != u[0] for u in usable)]
    if holes:
        # A listed-but-unusable strike inside the range is a gap in the ladder.
        # Interpolating across it would quietly invent the missing quote.
        return None, f"ladder holes at {', '.join(f'{k:g}' for k in holes[:4])}"

    return Smile(
        expiry_ts=expiry_ts, tau=tau, forward=forward,
        k_nodes=tuple(math.log(s / forward) for s, _ in usable),
        w_nodes=tuple(w for _, w in usable),
        n_listed=len(listed),
    ), "ok"


def build_smiles(chain: Chain, cfg: Config, now_ts: float | None = None
                 ) -> list[Smile]:
    """Every expiry that survives the quality gates, ascending in expiry."""
    return [s for s, _ in smile_report(chain, cfg, now_ts) if s is not None]


def smile_report(chain: Chain, cfg: Config, now_ts: float | None = None
                 ) -> list[tuple[Smile | None, str]]:
    """build_smiles plus the reason each rejected expiry was rejected (for the CLI)."""
    now_ts = now_ts if now_ts is not None else time.time()
    by_exp: dict[float, list[OptionQuote]] = {}
    for q in chain.quotes:
        by_exp.setdefault(q.expiry_ts, []).append(q)
    out = []
    for ts in sorted(set(by_exp) | set(chain.listed)):
        if (ts - now_ts) <= 0:
            continue
        out.append(_build_smile(ts, by_exp.get(ts, []),
                                chain.listed.get(ts, ()), cfg, now_ts))
    return out


def interpolate_variance(a: Smile, b: Smile, expiry_ts: float, now_ts: float,
                         min_nodes: int) -> Smile | None:
    """Smile at a date between two listed expiries, interpolated in TOTAL VARIANCE.

    This is the part most implementations get wrong. Interpolating the *price* or the
    *probability* linearly in time is wrong because uncertainty grows with the square
    root of time, not with time; interpolating the *vol* is wrong for the same reason.
    Total variance w = sigma^2 * tau is the quantity that is (near enough) linear in
    calendar time, so that is what gets interpolated - at fixed log-moneyness, with the
    forward interpolated log-linearly so the basis carries through too.
    """
    tau = (expiry_ts - now_ts) / _YEAR_SECONDS
    if not (a.tau < tau < b.tau) or b.tau <= a.tau:
        return None
    f = (tau - a.tau) / (b.tau - a.tau)
    forward = math.exp((1 - f) * math.log(a.forward) + f * math.log(b.forward))
    lo = max(a.k_nodes[0], b.k_nodes[0])
    hi = min(a.k_nodes[-1], b.k_nodes[-1])
    nodes = sorted({k for k in (*a.k_nodes, *b.k_nodes) if lo <= k <= hi})
    if len(nodes) < max(2, min_nodes):
        return None     # the two ladders barely overlap - not enough to price on
    return Smile(
        expiry_ts=expiry_ts, tau=tau, forward=forward,
        k_nodes=tuple(nodes),
        w_nodes=tuple((1 - f) * a._w(k) + f * b._w(k) for k in nodes),
        n_listed=min(a.n_listed, b.n_listed),
    )


# ------------------------------------------------------------------------- pricer


class DeribitPricer:
    """Live threshold probabilities for BTC/ETH. Returns None rather than a guess."""

    _CACHE_SECONDS = 300.0      # the surface moves, but not on a per-market timescale

    def __init__(self, cfg: Config, fetch=None) -> None:
        self.cfg = cfg
        self._fetch = fetch
        self._chains: dict[str, tuple[float, Chain]] = {}
        self._smiles: dict[str, tuple[float, list[Smile]]] = {}

    def supports(self, asset: str) -> bool:
        return bool(self.cfg.deribit_enabled) and asset.upper() in self.cfg.deribit_assets

    def chain(self, asset: str) -> Chain | None:
        asset = asset.upper()
        hit = self._chains.get(asset)
        if hit and time.time() - hit[0] < self._CACHE_SECONDS:
            return hit[1]
        try:
            ch = fetch_chain(self.cfg, asset, self._fetch)
        except Exception:  # noqa: BLE001 - upstream down means no number, not a guess
            return None
        self._chains[asset] = (time.time(), ch)
        return ch

    def smiles(self, asset: str, now_ts: float | None = None) -> list[Smile]:
        asset = asset.upper()
        now_ts = now_ts if now_ts is not None else time.time()
        hit = self._smiles.get(asset)
        if hit and abs(now_ts - hit[0]) < 60.0:
            return hit[1]
        ch = self.chain(asset)
        if ch is None:
            return []
        sm = build_smiles(ch, self.cfg, now_ts)
        self._smiles[asset] = (now_ts, sm)
        return sm

    def spot(self, asset: str) -> float | None:
        ch = self.chain(asset)
        if ch is None:
            return None
        if ch.index > 0:
            return ch.index
        fronts = sorted(ch.quotes, key=lambda q: q.expiry_ts)
        return fronts[0].forward if fronts else None

    def smile_for(self, asset: str, expiry: datetime,
                  now_ts: float | None = None) -> Smile | None:
        """The smile to price a given date on, exact or variance-interpolated."""
        if not self.supports(asset):
            return None
        now_ts = now_ts if now_ts is not None else time.time()
        target = expiry.timestamp()
        smiles = self.smiles(asset, now_ts)
        if not smiles:
            return None
        for s in smiles:
            if abs(s.expiry_ts - target) < 60.0:
                return s
        before = [s for s in smiles if s.expiry_ts < target]
        after = [s for s in smiles if s.expiry_ts > target]
        if not before or not after:
            return None     # outside the listed expiries - never extrapolate in time
        return interpolate_variance(before[-1], after[0], target, now_ts,
                                    self.cfg.deribit_min_strikes)

    def prob_above(self, asset: str, strike: float, expiry: datetime,
                   now_ts: float | None = None) -> float | None:
        s = self.smile_for(asset, expiry, now_ts)
        return s.digital(strike) if s is not None else None


# ---------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """python -m predictionedge.deribit [--asset BTC] [--series KXBTCD]

    Prints, for live Kalshi threshold markets, the Deribit-implied probability beside
    the old realized-vol lognormal number and the market's own price. The summary line
    at the end is the only question that matters: which model is closer to a liquid
    market's price.
    """
    import argparse

    from .config import Config as _Config
    from .crypto import CoinbaseData, deribit_prob_yes, event_time, model_prob_yes
    from .kalshi import LiveKalshiReadOnlyClient

    ap = argparse.ArgumentParser(prog="predictionedge.deribit")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--series", default="", help="Kalshi series (default KX<ASSET>D)")
    ap.add_argument("--max-spread-c", type=float, default=4.0,
                    help="only compare against Kalshi markets tighter than this")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args(argv)

    cfg = _Config.from_env()
    asset = args.asset.upper()
    series = args.series or f"KX{asset}D"
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()

    pricer = DeribitPricer(cfg)
    ch = pricer.chain(asset)
    if ch is None:
        print(f"deribit: could not fetch the {asset} chain")
        return 1
    print(f"{asset} chain: {len(ch.quotes)} two-sided quotes, "
          f"{len(ch.listed)} expiries, index={ch.index:,.0f}")
    print(f"gates: max_spread_frac={cfg.deribit_max_spread_frac} "
          f"min_strikes={cfg.deribit_min_strikes}\n")
    print(f"{'expiry':<26}{'days':>7}  {'fwd':>10}  status")
    for s, why in smile_report(ch, cfg, now_ts):
        if s is not None:
            print(f"{s.expiry_iso:<26}{s.tau * 365.25:>7.2f}  {s.forward:>10,.0f}  "
                  f"ok ({len(s.k_nodes)} strikes, "
                  f"{s.strikes[0]:,.0f}-{s.strikes[-1]:,.0f})")
        else:
            print(f"{'-':<26}{'':>7}  {'':>10}  REJECTED: {why}")

    kalshi = LiveKalshiReadOnlyClient(cfg.kalshi_base_url)
    try:
        markets = kalshi.list_markets(series_ticker=series, status="open", limit=500)
    except Exception as exc:  # noqa: BLE001
        print(f"\nkalshi: {exc}")
        return 1

    spot = CoinbaseData().spot(asset) or pricer.spot(asset) or 0.0
    vol = CoinbaseData().vol(asset)
    print(f"\nspot={spot:,.0f}  realized-vol={vol:.3f}" if vol else f"\nspot={spot:,.0f}")

    rows, failed = [], 0
    for m in markets:
        # A market nobody is quoting tightly is not a reference price, and comparing a
        # model against a 20c-wide book measures the book, not the model.
        if m.yes_bid <= 0 or m.yes_ask <= 0 or m.yes_ask <= m.yes_bid:
            continue
        if (m.yes_ask - m.yes_bid) * 100 > args.max_spread_c:
            continue
        # close_time, not expiration_time - see crypto.event_time for why.
        exp = event_time(m)
        if exp is None:
            continue
        tau = (exp - now).total_seconds() / _YEAR_SECONDS
        if tau <= 0:
            continue
        d = deribit_prob_yes(m, pricer, asset, exp, now_ts)
        if d is None:
            failed += 1
        ln = model_prob_yes(m, spot, vol, tau) if (spot and vol) else None
        strike = m.floor_strike if m.floor_strike is not None else m.cap_strike
        rows.append((m, float(strike or 0), d, ln, (m.yes_bid + m.yes_ask) / 2))

    print(f"\n{len(rows)} tightly-quoted {series} market(s); "
          f"deribit priced {len(rows) - failed}, failed closed on {failed}\n")
    print(f"{'ticker':<30}{'strike':>10}{'mkt':>7}{'deribit':>9}{'lognorm':>9}"
          f"{'d-err':>8}{'l-err':>8}")
    d_errs, l_errs = [], []
    for m, strike, d, ln, mid in (rows[:args.limit] if args.limit else rows):
        de = f"{abs(d - mid):.3f}" if d is not None else "  -  "
        le = f"{abs(ln - mid):.3f}" if ln is not None else "  -  "
        print(f"{m.ticker:<30}{strike:>10,.0f}{mid:>7.2f}"
              f"{(f'{d:.3f}' if d is not None else '   -'):>9}"
              f"{(f'{ln:.3f}' if ln is not None else '   -'):>9}{de:>8}{le:>8}")
    for m, strike, d, ln, mid in rows:
        if d is not None and ln is not None:
            d_errs.append(abs(d - mid))
            l_errs.append(abs(ln - mid))
    if d_errs:
        n = len(d_errs)
        print(f"\nhead-to-head on {n} market(s) both models priced:")
        print(f"  mean |deribit - market|   = {sum(d_errs) / n:.4f}")
        print(f"  mean |lognormal - market| = {sum(l_errs) / n:.4f}")
        print(f"  worst deribit   = {max(d_errs):.4f}")
        print(f"  worst lognormal = {max(l_errs):.4f}")
    else:
        print("\nno market was priced by both models - nothing to compare")
        print(f"  (Deribit's front expiries are usually too thin to clear the gates, so"
              f" hours-out\n   series fail closed. Try --series KX{asset}Y for a tenor"
              f" the option chain covers.)")
    print("\nADVISORY ONLY. Crypto stays paper; this changes the model, not the gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
