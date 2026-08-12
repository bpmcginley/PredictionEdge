"""The NBM station bulletin: parsing the thermometer's own guidance, cheaply.

Fixtures reproduce the live files byte-for-byte where alignment matters: the format
IS the column alignment (values right-aligned in 3-character fields under the UTC
row), and a fixture that "cleans it up" would test a file that does not exist.
"""

from datetime import datetime, timezone

from predictionedge.nbm import (
    parse_block, peek_cycle, station_block, update, value_for,
)

# Trimmed from the live 07Z file, 2026-08-12. TXN at a 00Z column is the max for the
# LOCAL day ending there: 86 under AUG 13's 00Z is Aug 12's max.
BLOCK_07Z = """\
 KNYC    NBM V5.0 NBS GUIDANCE    8/12/2026  0700 UTC
 DT /AUG  12      /AUG  13                /AUG  14                /AUG  15
 UTC  12 15 18 21 00 03 06 09 12 15 18 21 00 03 06 09 12 15 18 21 00 03 06
 FHR  05 08 11 14 17 20 23 26 29 32 35 38 41 44 47 50 53 56 59 62 65 68 71
 TXN              86          71          87          71          84
 XND               2           2           2           2           3
 TMP  73 80 84 86 80 77 75 73 74 80 84 85 81 77 75 72 74 80 84 84 79 75 72"""

# Trimmed from the live 13Z file, same day: the current day's max is ALREADY GONE
# (the first TXN value is the overnight min at 12Z). This is the fact that forces the
# cache to merge cycles rather than keep only the newest.
BLOCK_13Z = """\
 KNYC    NBM V5.0 NBS GUIDANCE    8/12/2026  1300 UTC
 DT /AUG  12/AUG  13                /AUG  14                /AUG  15
 UTC  18 21 00 03 06 09 12 15 18 21 00 03 06 09 12 15 18 21 00 03 06 09 12
 FHR  05 08 11 14 17 20 23 26 29 32 35 38 41 44 47 50 53 56 59 62 65 68 71
 TXN                    71          88          70          85          68
 XND                     2           2           2           3           3"""

NOW = datetime(2026, 8, 12, 17, 0, tzinfo=timezone.utc)


def _file(*blocks):
    return "\n\n".join(blocks) + "\n"


def _fetcher(files):
    """files: {url: text}. Missing url -> None (a 404, how the last part answers)."""
    calls = []

    def fetch(url, *, peek=False):
        calls.append((url, peek))
        text = files.get(url)
        if text is None:
            return None
        return text[:4096] if peek else text

    fetch.calls = calls
    return fetch


# --- parsing -----------------------------------------------------------------

def test_the_00z_column_is_the_max_for_the_local_day_that_ends_there():
    cycle, days = parse_block(BLOCK_07Z)
    assert cycle == "2026-08-12T07Z"
    assert days["2026-08-12"] == {"f": 86.0, "sd": 2.0}
    assert days["2026-08-13"] == {"f": 87.0, "sd": 2.0}
    assert days["2026-08-14"] == {"f": 84.0, "sd": 3.0}


def test_the_13z_run_has_already_dropped_the_current_day():
    """The overnight-min columns must not be mistaken for it either."""
    cycle, days = parse_block(BLOCK_13Z)
    assert cycle == "2026-08-12T13Z"
    assert "2026-08-12" not in days
    assert days["2026-08-13"]["f"] == 88.0


def test_a_missing_value_is_nothing_not_a_number():
    block = BLOCK_07Z.replace(" TXN              86 ", " TXN              MM ")
    _, days = parse_block(block)
    assert "2026-08-12" not in days and days["2026-08-13"]["f"] == 87.0


def test_the_year_rolls_over_a_december_january_boundary():
    block = """\
 KNYC    NBM V5.0 NBS GUIDANCE    12/31/2026  0700 UTC
 DT /DEC  31      /JAN   1                /JAN   2
 UTC  12 15 18 21 00 03 06 09 12 15 18 21 00 03 06
 TXN              41          28          39      """
    _, days = parse_block(block)
    assert days == {"2026-12-31": {"f": 41.0, "sd": None},
                    "2027-01-01": {"f": 39.0, "sd": None}}


def test_a_station_is_cut_from_the_file_at_the_blank_line():
    text = _file(BLOCK_07Z.replace("KNYC", "KABC"), BLOCK_07Z)
    blk = station_block(text, "KNYC")
    assert blk is not None and blk.count("GUIDANCE") == 1
    assert station_block(text, "KZZZ") is None


def test_peek_reads_the_stamp_from_the_first_header():
    assert peek_cycle(_file(BLOCK_07Z)[:200]) == "2026-08-12T07Z"
    assert peek_cycle("not a bulletin") is None


# --- the cache ---------------------------------------------------------------

URL = "https://www.weather.gov/source/mdl/NBM/blend_nbstx.t{:02d}z_{}.txt"


def test_cycles_merge_per_day_so_today_survives_the_13z_refresh():
    files = {URL.format(13, 1): _file(BLOCK_13Z),
             URL.format(7, 1): _file(BLOCK_07Z)}
    cache = {}
    assert update(cache, ["KNYC"], now=NOW, fetch=_fetcher(files)) == 2
    # Today only exists in the 07Z run; tomorrow takes the newer 13Z number.
    assert value_for(cache, "KNYC", "2026-08-12")["f"] == 86.0
    assert value_for(cache, "KNYC", "2026-08-12")["cycle"] == "2026-08-12T07Z"
    assert value_for(cache, "KNYC", "2026-08-13")["f"] == 88.0
    assert value_for(cache, "KNYC", "2026-08-13")["cycle"] == "2026-08-12T13Z"


def test_a_seen_cycle_costs_a_peek_and_nothing_more():
    files = {URL.format(13, 1): _file(BLOCK_13Z),
             URL.format(7, 1): _file(BLOCK_07Z)}
    fetch = _fetcher(files)
    cache = {}
    update(cache, ["KNYC"], now=NOW, fetch=fetch)
    before = len([c for c in fetch.calls if not c[1]])
    assert update(cache, ["KNYC"], now=NOW, fetch=fetch) == 0
    assert len([c for c in fetch.calls if not c[1]]) == before


def test_a_station_in_a_later_part_is_still_found():
    files = {URL.format(13, 1): _file(BLOCK_13Z.replace("KNYC", "KABC")),
             URL.format(13, 2): _file(BLOCK_13Z)}
    cache = {}
    update(cache, ["KNYC"], now=NOW, fetch=_fetcher(files), max_ingest=1)
    assert value_for(cache, "KNYC", "2026-08-13")["f"] == 88.0


def test_an_interrupted_download_is_retried_not_marked_seen():
    """Half a cycle must not count as the cycle: retry beats a silent gap."""
    def flaky(url, *, peek=False):
        if peek:
            return _file(BLOCK_13Z)[:4096]
        raise RuntimeError("connection reset")

    cache = {}
    assert update(cache, ["KNYC"], now=NOW, fetch=flaky) == 0
    assert cache["cycles_seen"] == []


def test_days_already_settled_are_pruned():
    files = {URL.format(13, 1): _file(BLOCK_13Z)}
    cache = {"stations": {"KNYC": {"2026-08-01": {"f": 90.0, "cycle": "old"}}},
             "cycles_seen": []}
    update(cache, ["KNYC"], now=NOW, fetch=_fetcher(files))
    assert value_for(cache, "KNYC", "2026-08-01") is None
    assert value_for(cache, "KNYC", "2026-08-13") is not None
