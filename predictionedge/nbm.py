"""NBM station guidance: the blend's forecast for the exact thermometer that settles.

`weather.py` prices off the NWS gridded forecast, which is a ~2.5km cell; settlement
is one named thermometer inside it, and the module docstring there calls the gap an
unmeasured bias. The National Blend of Models publishes STATION guidance - bias-
corrected to each thermometer's own history - as fixed-width text bulletins, and the
MDL site hosts the short-range (NBS) bulletin as plain static files:

    https://www.weather.gov/source/mdl/NBM/blend_nbstx.t{01,07,13,19}z_{1..N}.txt

Found by watching the network panel of the viewer at weather.gov/mdl/nbm_text: the
bulletin is split into ~8MB parts, alphabetical by station, and the viewer probes part
numbers until a 404. This is the only per-run-affordable route: api.weather.gov lists
the NBS/NBH/NBE product types but serves ZERO products for them (verified live), and
the NOMADS/AWS collectives are one ~30MB file per hourly cycle. Only the 01/07/13/19Z
cycles are hosted here, which is enough - the value moves four times a day.

Two facts about the bulletin shape everything below:

  * The TXN row is the max/min pair. A value in a column whose UTC row reads 00 is the
    daytime MAX for the LOCAL day that ends at that 00Z; a 12 column is the overnight
    min ending that morning. XND on the next row is the blend's own standard deviation
    for the same number.
  * A cycle only carries TXN for periods still ahead of it: the 13Z run has ALREADY
    DROPPED the current day's max (verified on the live file), so the noon run must
    read today's number from the 07Z or 01Z cycle. That is why the cache below MERGES
    cycles per target day instead of keeping only the newest run.

The filenames carry no date - `t13z` is whichever 13Z run was last uploaded, and the
hosting page warns it lags the operational stream - so the cycle identity is ALWAYS
the date stamp inside each station's own header line, never the filename or the clock.

Cost control: the cache (docs/nbm.json, committed by the workflow exactly like the
trial) records which cycle stamps it has ingested. Each run peeks at the first ~4KB of
each hour's part-1 file to read the stamp, and downloads bodies only for stamps it has
not seen - so the multi-MB parts move at most four times a day, not once per 15-minute
run.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from .weather import USER_AGENT

log = logging.getLogger(__name__)

NBM_DIR = "https://www.weather.gov/source/mdl/NBM"
CYCLE_HOURS = (1, 7, 13, 19)   # the only cycles the MDL page hosts
MAX_PARTS = 6                  # the viewer's own upper bound on split files
PEEK_BYTES = 4096
# At most this many new cycles ingested per run. After any outage the next runs walk
# the backlog two cycles at a time instead of downloading everything at once.
MAX_INGEST_PER_RUN = 2

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}

# Station IDs run past ICAO shapes: the first block of the live part-1 file is
# "086092", a 6-character numeric international ID.
_HEADER_RE = re.compile(
    r"^ ?([A-Z0-9]{4,8})\s+NBM V[\d.]+\s+NBS GUIDANCE\s+"
    r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{2})(\d{2}) UTC", re.MULTILINE)
# Date markers on the DT row: "/AUG  13". Their CHARACTER POSITION is the data - a
# marker applies to every column from the one it starts above onward.
_DT_RE = re.compile(r"/([A-Z]{3})\s+(\d{1,2})")


def _default_fetch(url: str, *, peek: bool = False) -> str | None:
    """GET a bulletin file; None on 404 (how the last part announces itself).

    `peek=True` reads only the first few KB - enough for the first station's header
    stamp - and drops the connection, so cycle-change detection costs KBs, not MBs.
    """
    import requests
    r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                     timeout=60, stream=peek)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    if peek:
        chunk = next(r.iter_content(PEEK_BYTES), b"") or b""
        r.close()
        return chunk.decode("utf-8", "replace")
    return r.text


def _part_url(hour: int, part: int) -> str:
    return f"{NBM_DIR}/blend_nbstx.t{hour:02d}z_{part}.txt"


def peek_cycle(text: str) -> str | None:
    """The cycle stamp of the first station header in `text`, e.g. `2026-08-12T07Z`."""
    m = _HEADER_RE.search(text or "")
    if not m:
        return None
    _, mo, dy, yr, hh, _ = m.groups()
    return f"{int(yr):04d}-{int(mo):02d}-{int(dy):02d}T{int(hh):02d}Z"


def station_block(text: str, station: str) -> str | None:
    """One station's bulletin, header line through the blank line that ends it."""
    m = re.search(rf"^ ?{re.escape(station)}\s+NBM V[\d.]+\s+NBS GUIDANCE.*$",
                  text, re.MULTILINE)
    if not m:
        return None
    end = re.search(r"\n[ \t]*\n", text[m.start():])
    return text[m.start():m.start() + end.start()] if end else text[m.start():]


def _row(lines: list[str], label: str) -> str | None:
    for ln in lines:
        if ln.strip().startswith(label + " ") or ln.strip() == label:
            return ln
    return None


def _cell(row: str | None, end: int) -> str:
    """The 3-character field that right-aligns at `end` (a UTC token's end index).

    Every data row aligns its values under the UTC row's hour tokens, right-aligned in
    3-character fields - that alignment, not any delimiter, is the file format.
    """
    if row is None:
        return ""
    return (row + " " * end)[max(0, end - 3):end].strip()


def parse_block(block: str, *, kind: str = "high") \
        -> tuple[str, dict[str, dict]] | None:
    """(cycle stamp, {local day: {"f": value, "sd": blend's own sd or None}}).

    For `kind="high"`: TXN at a 00Z column is the max for the local day ending there,
    so the local day is the column's date MINUS one. For `kind="low"`: TXN at a 12Z
    column is the overnight min ending that morning, which belongs to the column's own
    date. "MM" (missing) and blank cells yield nothing rather than a guess.
    """
    lines = block.splitlines()
    if not lines:
        return None
    header = _HEADER_RE.match(lines[0]) or _HEADER_RE.search(lines[0])
    if not header:
        return None
    _, mo, dy, yr, hh, _ = header.groups()
    cycle = f"{int(yr):04d}-{int(mo):02d}-{int(dy):02d}T{int(hh):02d}Z"

    dt_row, utc_row = _row(lines, "DT"), _row(lines, "UTC")
    txn_row, xnd_row = _row(lines, "TXN"), _row(lines, "XND")
    if dt_row is None or utc_row is None or txn_row is None:
        return None

    # Date markers, in order, rolling the year over a DEC -> JAN boundary.
    markers: list[tuple[int, date]] = []
    year, prev_month = int(yr), None
    for m in _DT_RE.finditer(dt_row):
        month = _MONTHS.get(m.group(1))
        if month is None:
            continue
        if prev_month is not None and month < prev_month:
            year += 1
        prev_month = month
        markers.append((m.start(), date(year, month, int(m.group(2)))))
    if not markers:
        return None

    out: dict[str, dict] = {}
    for tok in re.finditer(r"\d+", utc_row):
        hour, end = int(tok.group()), tok.end()
        col_dates = [d for s, d in markers if s < end]
        if not col_dates:
            continue
        col_date = col_dates[-1]
        if kind == "high" and hour == 0:
            local_day = col_date - timedelta(days=1)
        elif kind == "low" and hour == 12:
            local_day = col_date
        else:
            continue
        cell = _cell(txn_row, end)
        if not cell or cell == "MM":
            continue
        sd_cell = _cell(xnd_row, end)
        out[local_day.isoformat()] = {
            "f": float(cell),
            "sd": float(sd_cell) if sd_cell and sd_cell != "MM" else None,
        }
    return cycle, out


def value_for(cache: dict | None, station: str, day: str) -> dict | None:
    """The cached NBM entry for one station and local day, or None."""
    return (((cache or {}).get("stations") or {}).get(station) or {}).get(day)


def _hours_newest_first(now: datetime) -> list[int]:
    def last_occurrence(h: int) -> datetime:
        occ = now.replace(hour=h, minute=0, second=0, microsecond=0)
        return occ if occ <= now else occ - timedelta(days=1)
    return sorted(CYCLE_HOURS, key=last_occurrence, reverse=True)


def update(cache: dict, stations: list[str], *, kinds: dict[str, str] | None = None,
           now: datetime | None = None, fetch=None,
           max_ingest: int = MAX_INGEST_PER_RUN) -> int:
    """Fold any newly published cycles into `cache`. Returns cycles ingested.

    The cache merges PER TARGET DAY, newest cycle wins: the 13Z run refreshes tomorrow
    while today's number - which 13Z no longer carries - survives from the 07Z run.
    A cycle is marked seen even when a station is absent from it (the absence is in
    the file, and re-downloading will not change the file); it is NOT marked seen when
    a fetch dies mid-way, so an interrupted ingest retries on the next run.
    """
    getter = fetch or _default_fetch
    now = now or datetime.now(timezone.utc)
    per_station = cache.setdefault("stations", {})
    seen = cache.setdefault("cycles_seen", [])

    ingested = 0
    for hour in _hours_newest_first(now):
        if ingested >= max_ingest:
            break
        try:
            head = getter(_part_url(hour, 1), peek=True)
        except Exception:  # noqa: BLE001
            log.warning("NBM peek failed for %02dZ - skipping cycle", hour)
            continue
        stamp = peek_cycle(head or "")
        if not stamp or stamp in seen:
            continue

        remaining, blocks, interrupted = set(stations), {}, False
        for part in range(1, MAX_PARTS + 1):
            if not remaining:
                break
            try:
                text = getter(_part_url(hour, part))
            except Exception:  # noqa: BLE001
                log.warning("NBM part %d failed for %02dZ - will retry next run",
                            part, hour)
                interrupted = True
                break
            if text is None:               # past the last part
                break
            for stn in sorted(remaining):
                blk = station_block(text, stn)
                if blk is not None:
                    blocks[stn] = blk
                    remaining.discard(stn)
        if interrupted:
            continue

        for stn, blk in blocks.items():
            parsed = parse_block(blk, kind=(kinds or {}).get(stn, "high"))
            if parsed is None:
                continue
            blk_stamp, days = parsed
            entries = per_station.setdefault(stn, {})
            for day, val in days.items():
                cur = entries.get(day)
                if cur is None or str(cur.get("cycle", "")) < blk_stamp:
                    entries[day] = {**val, "cycle": blk_stamp}
        if remaining:
            log.warning("NBM %s missing station(s) %s", stamp, sorted(remaining))
        seen.append(stamp)
        ingested += 1

    # Prune: settled days are the weatherlog's business, not this cache's.
    cutoff = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    for stn in list(per_station):
        per_station[stn] = {d: v for d, v in per_station[stn].items() if d >= cutoff}
    cache["cycles_seen"] = seen[-16:]
    return ingested
