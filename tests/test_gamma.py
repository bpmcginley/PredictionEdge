from predictionedge.gamma import build_whale_map, resolve_condition_id


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
