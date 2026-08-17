"""Kalshi daily-temperature markets priced off the agency that settles them.

This is the only market family in the project where the SETTLEMENT SOURCE and a free
probabilistic forecast come from the same organisation. Kalshi settles KXHIGH*/KXLOW*
on the NWS Climatological Report (Daily) for one named station, and the NWS publishes
its own gridded forecast for that station's location at no cost and no API key. There
is no vendor in between to disagree with.

It is also the only family here that is BOTH liquid and tradeable by a US person.
Measured 2026-08-11 on the NYC ladder: 1c spreads on near-the-money brackets, 900-9,300
contracts of open interest, 1,510 resting at the bid. And it settles DAILY, which is
what makes it worth building - the Polymarket board needs roughly six months to reach a
sample that can separate a real edge from luck, and a market that resolves every day
gets there in weeks.

*** NOTHING HERE IS VALIDATED. *** Tickets are paper-only by construction: they carry
`venue="kalshi"` into the existing paper trial and no order path reads them. The model
below is a normal distribution around a deterministic forecast, which is a crude thing
to point at a real market, and the whole reason for the trial is to find out how crude.

THE FAILURE MODE THIS MODULE IS BUILT AROUND. A too-tight sigma does not produce a
slightly wrong model, it INVENTS EDGE - every centre bracket reads overpriced and every
tail reads cheap, uniformly, and the result looks like a systematic discovery rather
than a mistake. This project has already made that exact error once: `macrofv` shipped
with a guessed CPI-nowcast sigma of 0.12 when the published verification error was
0.16, and the corrected number deleted the edges. So the sigma here is defended three
ways, in increasing order of how much they can be trusted:

  1. The model does not choose its own width AT ALL. The ladder's own prices set sigma
     (`pricing_sigma`); the forecast supplies only the location of the distribution.
     A prior cannot fabricate a trade if no trade is priced off it.
  2. The prior survives solely as a plausibility bound on the market's width, so a
     stale or broken book is skipped rather than traded against.
  3. Every (forecast, observed) pair is recorded so the width can eventually be
     replaced by a measured one. Until that happens nothing here is calibrated and
     every ticket says so.

AND NOTE WHAT WAS WRONG. An earlier draft of this docstring claimed the asymmetry ran
one way - "too tight fabricates trades, too wide merely finds fewer of them" - and the
tests disproved it within the hour. A too-wide sigma pushes mass into the TAILS and
makes every tail bracket look cheap, which fabricates trades exactly as reliably. There
is no safe direction to be wrong about width, which is why the market now supplies it.

GRID IS NOT STATION. The NWS gridded forecast is for a ~2.5km grid cell; settlement
is one thermometer inside it. Central Park runs cooler than the surrounding grid on
calm sunny days, Midway differs from O'Hare, and no hand-tuned correction is applied
here - a guessed bias would be the same mistake as a guessed sigma, wearing a lab
coat. Instead the mean now comes from the NBM STATION bulletin (`nbm.py`) whenever
one is cached for the target day: the blend is statistically corrected to the exact
settling thermometer's own history, which removes the grid-versus-station gap at the
source instead of guessing at it. The gridpoint value remains the fallback and is
recorded alongside on every station-priced ticket, so the size of the gap this switch
closed becomes a measured number instead of folklore.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import calibration
from .fees import fee_per_contract
# Re-exported, not just used: `weatherlog` imports `_event_day` from here. It is the
# one shared Kalshi ticker parser now - this module used to carry its own copy, which
# was correct only because the weather ladders happen to hang no suffix off the day.
from .kalshi import event_day as _event_day

log = logging.getLogger(__name__)

NWS_API = "https://api.weather.gov"
# The NWS asks for a contact in the User-Agent and throttles anonymous callers.
USER_AGENT = "PredictionEdge/1.0 (github.com/bpmcginley/PredictionEdge)"


@dataclass(frozen=True)
class WeatherCity:
    """One Kalshi series and the exact thermometer that settles it."""
    series: str          # Kalshi series ticker, e.g. KXHIGHNY
    name: str            # settlement location as Kalshi words it
    wfo: str             # NWS forecast office, for the CLI product URL
    cli: str             # `issuedby` code in the CLI product URL
    lat: float
    lon: float
    kind: str = "high"   # "high" | "low"
    station: str = ""    # NBM bulletin station ID; "" means no station guidance

    @property
    def cli_url(self) -> str:
        """The document that decides the market. Worth printing next to a ticket."""
        return (f"https://forecast.weather.gov/product.php?"
                f"site={self.wfo}&product=CLI&issuedby={self.cli}")


# HAND-AUDITED, and it stays that way. Each entry was read out of that series' own
# settlement rules on the Kalshi API, not inferred from the city name - because the
# obvious inference is wrong at least once: Chicago settles on MIDWAY, not O'Hare, and
# a model forecasting the wrong airport would be measuring a different variable while
# looking completely healthy. This is the same reason `consistency.py` refuses to pair
# events automatically. Add a city only after reading its rules text.
CITIES: dict[str, WeatherCity] = {
    "KXHIGHNY": WeatherCity("KXHIGHNY", "Central Park, New York", "OKX", "NYC",
                            40.7789, -73.9692, station="KNYC"),
    "KXHIGHCHI": WeatherCity("KXHIGHCHI", "Chicago Midway, IL", "LOT", "MDW",
                             41.7868, -87.7522, station="KMDW"),
    "KXHIGHMIA": WeatherCity("KXHIGHMIA", "Miami International Airport", "MFL", "MIA",
                             25.7932, -80.2906, station="KMIA"),
    "KXHIGHDEN": WeatherCity("KXHIGHDEN", "Denver, CO", "BOU", "DEN",
                             39.8561, -104.6737, station="KDEN"),
}

# Prior standard deviation of the NWS max-temperature forecast error, in degrees F,
# for a same-day forecast, plus growth per additional day of lead.
#
# THESE ARE A PRIOR, NOT A MEASUREMENT, and they are set wide on purpose. Published
# NWS/NBM day-1 max-temperature MAE sits around 2-3F at well-observed stations, which
# implies a sigma near 3-4F; this starts at the top of that range and adds a full
# degree per day, because on top of the forecast's own error sits an unmeasured
# grid-versus-station bias (see the module docstring) that has to go somewhere. When
# the trial has enough settled rows, replace these with a fitted per-city value.
SIGMA_DAY0_F = 4.0
SIGMA_GROWTH_F = 1.0
# Never trust a sigma below this no matter what any future fit says. A forecast is not
# a thermometer; claiming better than ~2F on a daily high is claiming to know the
# weather better than the agency that measures it.
SIGMA_FLOOR_F = 2.0


def c_to_f(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


def _cdf(x: float, mu: float, sigma: float) -> float:
    """Normal CDF. `math.erf` keeps this stdlib-only, like the rest of the project."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def bracket_probability(floor_strike: float | None, cap_strike: float | None,
                        mu: float, sigma: float) -> float | None:
    """P(the reported high lands in this bracket), or None if the bracket is unreadable.

    THE HALF-DEGREE IS NOT A ROUNDING DETAIL, it is most of the answer on a narrow
    bracket. The CLI report states a whole number of degrees F, so "85-86" is the event
    {85, 86} and not the interval [85, 86] - a continuous reading of it spans 1 degree
    where the real event spans 2, understating a centre bracket by roughly half and
    making every one of them look overpriced. Bounds therefore sit on the .5 between
    integers. `>90` means 91 or above (bound 90.5) and `<83` means 82 or below
    (bound 82.5), matching how Kalshi words the subtitles.
    """
    if floor_strike is None and cap_strike is None:
        return None
    if floor_strike is not None and cap_strike is not None:
        lo, hi = float(floor_strike) - 0.5, float(cap_strike) + 0.5
        if hi <= lo:
            return None
        return max(0.0, _cdf(hi, mu, sigma) - _cdf(lo, mu, sigma))
    if cap_strike is not None:                       # "< cap": cap-1 or below
        return max(0.0, _cdf(float(cap_strike) - 0.5, mu, sigma))
    return max(0.0, 1.0 - _cdf(float(floor_strike) + 0.5, mu, sigma))


def _bracket_centre(floor_strike, cap_strike) -> float | None:
    """A representative temperature for a bracket, for reading the market's own view."""
    if floor_strike is not None and cap_strike is not None:
        return (float(floor_strike) + float(cap_strike)) / 2.0
    # Open-ended buckets have no centre; 2F past the edge is a deliberate understatement
    # of how far a tail can run, which makes the implied sigma CONSERVATIVE (smaller),
    # and the only use of that number is as a floor on our own.
    if cap_strike is not None:
        return float(cap_strike) - 2.0
    if floor_strike is not None:
        return float(floor_strike) + 2.0
    return None


def market_distribution(ladder: list[dict]) -> tuple[float, float] | None:
    """Mean and standard deviation implied by a ladder's own prices, or None.

    Returns None unless the brackets form a usable partition. That check is the same
    lesson `odds.require_complete_market` encodes: de-vigging an INCOMPLETE market
    inflated a favourite by 11 points because a missing leg let the survivors sum to
    less than one. Here an incomplete ladder would understate the market's width and
    hand back a sigma floor that is too low - which is precisely the failure this
    module exists to avoid.
    """
    pts: list[tuple[float, float]] = []
    for m in ladder:
        price = m.get("mid")
        centre = _bracket_centre(m.get("floor_strike"), m.get("cap_strike"))
        if price is None or centre is None:
            continue
        pts.append((centre, float(price)))
    total = sum(p for _, p in pts)
    # A complete, sanely-quoted ladder sums to about 1. Anything outside this band means
    # legs are missing or the book is stale, and neither is a distribution worth reading.
    # The band is deliberately tight: a ladder missing its smallest tail leg still sums
    # to ~0.95, and a 15% tolerance would wave that through while the missing mass
    # quietly shrinks the implied sigma - which is the one number this module cannot
    # afford to underestimate, since it is now the ONLY source of width.
    if len(pts) < 3 or not 0.90 <= total <= 1.10:
        return None
    mean = sum(c * p for c, p in pts) / total
    var = sum(p * (c - mean) ** 2 for c, p in pts) / total
    return mean, math.sqrt(var)


def sigma_for_lead(days_ahead: float) -> float:
    """The PRIOR width. Used only as a sanity bound - see `pricing_sigma`."""
    return SIGMA_DAY0_F + SIGMA_GROWTH_F * max(0.0, days_ahead)


def pricing_sigma(market_sigma: float | None, days_ahead: float) -> float | None:
    """Width comes from the MARKET; the forecast only supplies the location.

    An earlier version of this took the wider of the model's prior and the market's
    implied width, on the reasoning that a too-wide sigma is the safe direction. That
    reasoning is WRONG and the tests caught it: a too-wide sigma pushes mass into the
    tails and makes every tail bracket look cheap, manufacturing edge exactly as
    reliably as a too-tight sigma manufactures it in the centre. Fed the market's own
    mean, that version still printed 27c of "edge" on a 5c tail. There is no safe
    direction to be wrong about width.

    So the hypothesis is narrowed to the only one worth testing and the only one this
    data supports: *the NWS forecast locates tomorrow's high better than the market's
    implied mean does*. Width is not contested - the ladder's own prices are the best
    available estimate of it, they come from people with money at stake, and using them
    means a wrong prior can no longer create a trade.

    The prior survives only as a plausibility bound. A ladder implying it knows
    tomorrow to a quarter of a degree is quoting a stale or broken book, not a
    conviction, and pricing against it would hand back enormous fake edge.
    """
    if market_sigma is None:
        return None                      # no readable width: no opinion, not a default
    prior = sigma_for_lead(days_ahead)
    if market_sigma < SIGMA_FLOOR_F:
        log.info("market sigma %.2fF below floor - widening to %.1fF",
                 market_sigma, SIGMA_FLOOR_F)
        return SIGMA_FLOOR_F
    # Far wider than any plausible forecast error means the ladder is not pricing a
    # weather forecast at all. Suppress rather than trade against a book we cannot read.
    if market_sigma > prior * 2.5:
        log.warning("market sigma %.2fF implausible vs prior %.2fF - skipping ladder",
                    market_sigma, prior)
        return None
    return market_sigma


def forecast_highs(city: WeatherCity, *, fetch=None) -> dict[str, float]:
    """{YYYY-MM-DD: forecast high in F} from the NWS gridded forecast.

    Two calls: the point lookup gives the grid cell, the grid gives the series. Values
    arrive in Celsius under `wmoUnit:degC` regardless of locale, and each carries an
    ISO8601 interval whose START date is the day the high belongs to.
    """
    getter = fetch or _default_fetch
    point = getter(f"{NWS_API}/points/{city.lat},{city.lon}")
    grid_url = (point.get("properties") or {}).get("forecastGridData")
    if not grid_url:
        log.warning("no forecastGridData for %s", city.series)
        return {}
    grid = getter(grid_url)
    field = "maxTemperature" if city.kind == "high" else "minTemperature"
    block = ((grid.get("properties") or {}).get(field)) or {}
    celsius = "degc" in str(block.get("uom", "")).lower()
    out: dict[str, float] = {}
    for v in block.get("values") or []:
        stamp, value = v.get("validTime", ""), v.get("value")
        if value is None or "/" not in stamp:
            continue
        day = stamp.split("T")[0]
        temp = c_to_f(float(value)) if celsius else float(value)
        # First reading for a day wins; the series is ordered and a later duplicate
        # would be a revision of a day already past.
        out.setdefault(day, temp)
    if not out:
        log.warning("no %s values for %s", field, city.series)
    return out


def _default_fetch(url: str, params: dict | None = None) -> dict:
    import requests
    r = requests.get(url, params=params or {},
                     headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
                     timeout=20)
    r.raise_for_status()
    return r.json()


# A ratio between two normal tail probabilities can run to millions when the forecast
# sits far outside the ladder. Capping it bounds how much one leg can dominate the
# renormalisation; it never binds on a forecast the market would recognise.
_MAX_TILT = 1000.0


def price_ladder(ladder: list[dict], forecast_f: float, *, days_ahead: float,
                 min_edge: float = 0.07, min_price: float = 0.05,
                 max_price: float = 0.95) -> list[dict]:
    """Score one day's brackets against the forecast. Returns candidate tickets.

    THE MARKET'S PRICES ARE THE PRIOR; the forecast only TILTS them. Each bracket's
    quoted mid is multiplied by the likelihood ratio of that bracket under the forecast
    versus under the market's own implied mean, at a shared sigma, and the results are
    rescaled back to the mass the market quotes.

    That indirection is the whole design, and it is here because the obvious version
    failed. Pricing brackets directly off a normal centred on the forecast compares two
    different SHAPES as well as two different means: a real ladder is fatter in the
    centre and thinner in the tails than the moment-matched normal, so even when fed the
    market's own mean the direct version printed 8c of "edge" on the bottom bracket - a
    pure artefact of the normal, indistinguishable in the output from a real signal.
    Under the ratio, the shape error appears in the numerator and denominator alike and
    cancels. When the forecast equals the market's mean every ratio is exactly 1 and
    every edge is exactly minus the half-spread, so agreement CANNOT produce a ticket.
    That is a structural guarantee rather than a threshold that happens to hold.

    Rescaling to the market's own total (not to 1.0) keeps the tilt from inventing mass:
    a shifted mean redistributes probability between brackets, it does not create any.

    `min_price`/`max_price` exclude the extremes deliberately. The normal is at its least
    trustworthy in the tails, a 2c market cannot be beaten by 7c of claimed edge without
    the claim being about the model rather than the market, and the fee on a 2c contract
    eats the entire position.
    """
    market = market_distribution(ladder)
    if market is None:
        return []                      # incomplete or stale ladder: no opinion, no ticket
    market_mu, market_sigma = market
    sigma = pricing_sigma(market_sigma, days_ahead)
    if sigma is None:
        return []                      # unreadable width: no opinion, no ticket

    tilted: list[tuple[dict, float]] = []
    for m in ladder:
        mid, strikes = m.get("mid"), (m.get("floor_strike"), m.get("cap_strike"))
        if mid is None:
            continue
        under_forecast = bracket_probability(*strikes, forecast_f, sigma)
        under_market = bracket_probability(*strikes, market_mu, sigma)
        if under_forecast is None or under_market is None:
            continue
        ratio = min(under_forecast / under_market, _MAX_TILT) \
            if under_market > 0 else (_MAX_TILT if under_forecast > 0 else 0.0)
        tilted.append((m, float(mid) * ratio))

    quoted = sum(float(m["mid"]) for m, _ in tilted)
    shifted = sum(p for _, p in tilted)
    if shifted <= 0 or quoted <= 0:
        return []                      # the forecast is off every bracket: unpriceable
    scale = quoted / shifted

    out: list[dict] = []
    for m, raw in tilted:
        ask = m.get("ask")
        if ask is None or not min_price <= ask <= max_price:
            continue
        p = raw * scale
        edge = p - ask
        if edge < min_edge:
            continue
        out.append({
            "market_id": m["ticker"],
            "venue": "kalshi",
            "source": "weather",
            "outcome": "Yes",
            "title": m.get("title", ""),
            "url": m.get("url", ""),
            "entry_price": round(ask, 4),
            # The best bid rides along so the paper trial can simulate a maker-first
            # entry (join the bid, wait) instead of assuming a taker cross at the ask.
            "bid": round(float(m["bid"]), 4) if m.get("bid") is not None else None,
            "model_prob": round(p, 4),
            "edge": round(edge, 4),
            # Everything below exists so the prior can be re-fit later against what
            # actually happened, and so a reader can tell WHY a ticket appeared.
            "forecast_f": round(forecast_f, 2),
            "sigma_f": round(sigma, 2),
            "market_mu_f": round(market_mu, 2),
            "market_sigma_f": round(market_sigma, 2),
            "days_ahead": round(days_ahead, 2),
            "market_prob": round(float(m["mid"]), 4),
            "model": "kalshi-ladder-tilted-by-nws-gridpoint",
            "validated": False,
        })
    return out


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def _hours_to_expiry(close_iso: str, day: str, now: datetime) -> float:
    """Horizon for the calibration overlay, from the market's own close time.

    Falls back to the end of the target day (UTC) when the API serves no parseable
    close_time - for a daily-high market that is within hours of the truth, and the
    overlay's horizon buckets are 48h wide, so the fallback cannot change a bucket
    except at a boundary the real close was already straddling.
    """
    when = None
    if close_iso:
        try:
            when = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            when = None
    if when is None:
        when = datetime.strptime(day, "%Y-%m-%d").replace(
            tzinfo=timezone.utc) + timedelta(days=1)
    return max(0.0, (when - now).total_seconds() / 3600.0)


def _calibration_gate(t: dict, hours: float, *, shrink: float,
                      diag: dict | None = None) -> bool:
    """Apply the overlay to one candidate ticket. True = keep (and annotate).

    The veto logic lives in `calibration.assess`; this is only the weather wiring:
    fair = the tilted model probability, quote = the ask we would pay, fee threshold =
    the taker fee at that price (a weather entry takes the ask - see `papertrial`).
    A suppressed ticket is counted, not logged away: `cal_vetoed` lands in the
    published diagnostics so the rate of vetoes is visible without grepping logs.
    """
    a = calibration.assess(t["model_prob"], t["entry_price"], "weather", hours,
                           shrink=shrink,
                           min_abs_edge=fee_per_contract(t["entry_price"], maker=False))
    if not a.ok:
        log.info("calibration veto (%s): %s edge=%+.3f cal_edge=%+.3f",
                 a.reason, t.get("market_id", "?"), t["edge"], a.cal_edge)
        if diag is not None:
            diag["cal_vetoed"] = diag.get("cal_vetoed", 0) + 1
        return False
    t["cal_prob"] = round(a.cal_prob, 4)
    t["cal_edge"] = round(a.cal_edge, 4)
    t["cal_b"] = round(a.cal_b, 4)
    return True


def find_weather_edges(*, cities: dict[str, WeatherCity] | None = None,
                       kalshi_fetch=None, nws_fetch=None,
                       nbm_cache: dict | None = None, nbm_fetch=None,
                       max_days: float = 5.0, min_edge: float = 0.07,
                       cal_overlay: bool = True,
                       cal_shrink: float = calibration.DEFAULT_SHRINK,
                       diag: dict | None = None,
                       now: datetime | None = None) -> list[dict]:
    """Every bracket whose model probability beats its ask by `min_edge`.

    Paper-only by construction: tickets are stamped `venue="kalshi"` for the trial and
    nothing in the order path consumes this function.

    `nbm_cache` is the persisted station-guidance cache from `nbm.py`, mutated in
    place (the caller owns saving it). When it holds a value for a city's station and
    target day, that value is the mean and the gridpoint is only recorded for
    comparison; with no cache or no value, the gridpoint prices the day as before.

    `cal_overlay` runs each candidate through the calibration sanity layer
    (`calibration.py`): a ticket whose edge does not survive correcting the market's
    published miscalibration out of its price is suppressed, and the count lands in
    `diag["cal_vetoed"]` when the caller supplies a dict. Survivors carry `cal_prob`,
    `cal_edge` and `cal_b` so the trial can score the veto rule itself later.
    """
    from .kalshi import KALSHI_API, _default_fetch as kx_fetch, _price_to_dollars

    getter = kalshi_fetch or kx_fetch
    now = now or datetime.now(timezone.utc)
    tickets: list[dict] = []

    city_map = cities or CITIES
    if nbm_cache is not None:
        from . import nbm
        stations = [c.station for c in city_map.values() if c.station]
        try:
            nbm.update(nbm_cache, stations,
                       kinds={c.station: c.kind for c in city_map.values()
                              if c.station},
                       now=now, fetch=nbm_fetch)
        except Exception:  # noqa: BLE001
            # Fail OPEN to the gridpoint: a broken bulletin host must not silence the
            # sleeve, it just prices the old way. Whatever the cache already holds is
            # still used - stale station guidance is caught per-day by the cycle merge.
            log.warning("NBM update failed - gridpoint only this run", exc_info=True)

    for city in city_map.values():
        try:
            highs = forecast_highs(city, fetch=nws_fetch)
        except Exception:  # noqa: BLE001
            log.warning("NWS gridpoint unavailable for %s", city.series)
            highs = {}         # the station guidance, if cached, can still price days
        station_days = (((nbm_cache or {}).get("stations") or {})
                        .get(city.station) or {})
        if not highs and not station_days:
            continue           # fail closed: no forecast is not a free opinion
        try:
            # No `status` filter, for the reason `kalshi.market_meta` documents at
            # length: pinning it hides markets and the hiding is silent.
            data = getter(f"{KALSHI_API}/markets",
                          {"series_ticker": city.series, "limit": 200})
        except Exception:  # noqa: BLE001
            log.warning("kalshi ladder unavailable for %s - skipping", city.series)
            continue

        by_event: dict[str, list[dict]] = {}
        for m in data.get("markets") or []:
            if m.get("status") not in ("active", "open"):
                continue
            bid, ask = _price_to_dollars(m, "yes_bid"), _price_to_dollars(m, "yes_ask")
            by_event.setdefault(m.get("event_ticker", ""), []).append({
                "ticker": m.get("ticker", ""),
                "title": m.get("title", ""),
                "url": f"https://kalshi.com/markets/{m.get('event_ticker', '')}",
                "floor_strike": m.get("floor_strike"),
                "cap_strike": m.get("cap_strike"),
                "bid": bid, "ask": ask, "mid": _mid(bid, ask),
                "close_time": m.get("close_time", ""),
            })

        for event, ladder in by_event.items():
            day = _event_day(event)
            if not day:
                continue
            ent = station_days.get(day)
            if ent is None and day not in highs:
                continue
            days_ahead = (datetime.strptime(day, "%Y-%m-%d").replace(
                tzinfo=timezone.utc) - now).total_seconds() / 86400.0
            if days_ahead < -0.5 or days_ahead > max_days:
                continue
            mu = float(ent["f"]) if ent is not None else highs[day]
            close_iso = next((leg.get("close_time") for leg in ladder
                              if leg.get("close_time")), "")
            hours = _hours_to_expiry(close_iso, day, now)
            for t in price_ladder(ladder, mu, days_ahead=max(0.0, days_ahead),
                                  min_edge=min_edge):
                if cal_overlay and not _calibration_gate(t, hours, shrink=cal_shrink,
                                                         diag=diag):
                    continue
                if ent is not None:
                    t["model"] = "kalshi-ladder-tilted-by-nbm-station"
                    t["forecast_src"] = "nbm-station"
                    t["nbm_cycle"] = ent.get("cycle", "")
                    if ent.get("sd") is not None:
                        # The blend's own width for this value. NOT used to price -
                        # the market still supplies sigma - recorded for the re-fit.
                        t["nbm_sd_f"] = float(ent["sd"])
                    if day in highs:
                        # The number the grid cell would have said, so the size of
                        # the grid-vs-station gap becomes measurable from the trial.
                        t["forecast_grid_f"] = round(highs[day], 2)
                else:
                    t["forecast_src"] = "gridpoint"
                t["city"] = city.name
                t["settles_by"] = city.cli_url
                t["end_iso"] = (datetime.strptime(day, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc) + timedelta(days=1)).isoformat()
                t["event_iso"] = t["end_iso"]
                tickets.append(t)

    tickets.sort(key=lambda t: t["edge"], reverse=True)
    return tickets
