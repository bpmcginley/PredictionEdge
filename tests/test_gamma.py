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
