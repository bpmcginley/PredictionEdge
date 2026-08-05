from predictionedge.pmus_match import PMUSMatcher, slug_signature


def test_signature_extracts_teams_date_outcome():
    assert slug_signature("fifwc-ecu-ger-2026-06-25-ger") == (
        frozenset({"ecu", "ger"}), "2026-06-25", "ger")


def test_match_across_prefixes():
    m = PMUSMatcher(["atc-fwc-ecu-ger-2026-06-25-ger",
                     "atc-fwc-ecu-ger-2026-06-25-draw"])
    assert m.match("fifwc-ecu-ger-2026-06-25-ger") == "atc-fwc-ecu-ger-2026-06-25-ger"
    assert m.match("fifwc-ecu-ger-2026-06-25-draw") == "atc-fwc-ecu-ger-2026-06-25-draw"


def test_no_match_for_different_game():
    m = PMUSMatcher(["atc-fwc-bra-arg-2026-06-25-bra"])
    assert m.match("fifwc-ecu-ger-2026-06-25-ger") is None


def test_signature_none_without_date():
    assert slug_signature("some-market-without-date") is None
    assert slug_signature("") is None
