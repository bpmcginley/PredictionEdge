from predictionedge.gamma import build_whale_map, market_meta, resolve_condition_id


def _fake(payload):
    def fetch(url, params):
        return payload
    return fetch


def test_resolve_list_shape():
    assert resolve_condition_id("s", fetch=_fake([{"conditionId": "0xABC"}])) == "0xABC"


def test_resolve_dict_shape():
    assert resolve_condition_id("s", fetch=_fake({"data": [{"condition_id": "0xDEF"}]})) == "0xDEF"


def test_resolve_none_when_missing():
    assert resolve_condition_id("s", fetch=_fake([])) is None


def test_resolve_handles_fetch_error():
    def boom(url, params):
        raise RuntimeError("network down")
    assert resolve_condition_id("s", fetch=boom) is None


def test_build_map():
    payloads = {"slugA": [{"conditionId": "0x1"}], "slugB": [{"conditionId": "0x2"}]}
    def fetch(url, params):
        return payloads[params["slug"]]
    mapping = build_whale_map({"KX-A": "slugA", "KX-B": "slugB"}, fetch=fetch)
    assert mapping == {"KX-A": "0x1", "KX-B": "0x2"}


def test_game_start_never_falls_back_to_creation_date():
    """`startDate` is when the MARKET was created, so it is always in the past.

    Reading it as kickoff made every market without a fixture - politics, crypto,
    econ, world events - look like it had already started.
    """
    meta = market_meta(["0x1"], fetch=_fake([{
        "conditionId": "0x1", "question": "Fed cuts in September?",
        "startDate": "2026-01-01T00:00:00Z", "endDate": "2026-09-18T00:00:00Z",
    }]))["0x1"]
    assert meta["game_start"] == ""                      # absent is absent
    assert meta["created_at"] == "2026-01-01T00:00:00Z"  # kept, under its real name


def test_game_start_is_used_when_the_venue_gives_one():
    meta = market_meta(["0x1"], fetch=_fake([{
        "conditionId": "0x1", "question": "Mets vs Pirates",
        "startDate": "2026-07-01T00:00:00Z",
        "gameStartTime": "2026-08-07T22:40:00Z",
        "endDate": "2026-08-14T22:40:00Z",
    }]))["0x1"]
    assert meta["game_start"] == "2026-08-07T22:40:00Z"


def _row(cid):
    return {"conditionId": cid, "question": cid, "endDate": "2026-09-01T00:00:00Z"}


def test_oversized_batch_is_bisected_rather_than_dropped():
    """Gamma rejecting a batch must cost calls, not markets.

    The old code swallowed a failed chunk whole, so 20 signals at a time disappeared
    under a rejection reason ("no market metadata") that was simply untrue.
    """
    def fetch(url, params):
        cids = [v for k, v in params if k == "condition_ids"]
        if len(cids) > 2:
            raise RuntimeError("414 request too long")
        return [_row(c) for c in cids]

    failures: set[str] = set()
    ids = [f"0x{i}" for i in range(9)]
    meta = market_meta(ids, fetch=fetch, failures=failures)
    assert set(meta) == set(ids)
    assert failures == set()


def test_ids_that_never_read_are_reported_not_assumed_absent():
    """A market Gamma could not be ASKED about is not a market Gamma denied."""
    def fetch(url, params):
        cids = [v for k, v in params if k == "condition_ids"]
        if "0xBAD" in cids:
            raise RuntimeError("connection reset")
        return [_row(c) for c in cids]

    failures: set[str] = set()
    meta = market_meta(["0xA", "0xBAD", "0xC"], fetch=fetch, failures=failures)
    assert set(meta) == {"0xA", "0xC"}
    assert failures == {"0xBAD"}


def test_absent_market_is_not_reported_as_a_failure():
    """Gamma answering "no such market" is a clean answer, and must not read as a fault."""
    failures: set[str] = set()
    meta = market_meta(["0xGONE"], fetch=_fake([]), failures=failures)
    assert meta == {} and failures == set()


def _gamma_like(closed_ids):
    """Stand-in for /markets, which hides closed markets unless asked for them."""
    calls = []

    def fetch(url, params):
        cids = [v for k, v in params if k == "condition_ids"]
        want_closed = any(k == "closed" and v == "true" for k, v in params)
        calls.append((tuple(cids), want_closed))
        return [{"conditionId": c, "question": c, "closed": c in closed_ids,
                 "endDate": "2026-08-11T00:00:00Z"}
                for c in cids if (c in closed_ids) == want_closed]

    return fetch, calls


def test_resolved_markets_are_fetched_not_reported_missing():
    """`/markets` defaults to closed=false, so a RESOLVED market is withheld, not absent.

    Measured against the live feed on 2026-08-11: 146 of 461 ids came back empty and
    every one of them returned under `closed=true`. Without the second pass the board
    calls that "no market metadata" - and papertrial can never settle at all, because
    the only markets it asks about are the ones that resolved.
    """
    fetch, calls = _gamma_like({"0xB"})
    failures: set[str] = set()
    meta = market_meta(["0xA", "0xB"], fetch=fetch, failures=failures)
    assert set(meta) == {"0xA", "0xB"}
    assert meta["0xB"]["closed"] is True
    assert failures == set()
    # Only the unanswered id is re-asked; the open pass is not paid for twice.
    assert calls[-1] == (("0xB",), True)


def test_no_second_pass_when_the_open_pass_answered_everything():
    fetch, calls = _gamma_like(set())
    market_meta(["0xA", "0xB"], fetch=fetch)
    assert len(calls) == 1 and calls[0][1] is False
