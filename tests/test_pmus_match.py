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


# Every slug below is copied from a real published board, not invented. The old
# length-filtered signature returned None for all three tennis ones.
def test_surname_slugs_are_routable():
    assert slug_signature("wta-annli-rybakin-2026-08-07") == (
        frozenset({"annli", "rybakin"}), "2026-08-07", "")
    assert slug_signature("atp-grieksp-arnaldi-2026-08-07") == (
        frozenset({"grieksp", "arnaldi"}), "2026-08-07", "")


def test_short_code_slugs_still_work():
    assert slug_signature("mlb-nym-pit-2026-08-07") == (
        frozenset({"nym", "pit"}), "2026-08-07", "")
    assert slug_signature("mlb-hou-sd-2026-08-07") == (
        frozenset({"hou", "sd"}), "2026-08-07", "")


def test_a_tennis_market_finds_its_us_counterpart():
    m = PMUSMatcher(["atc-atp-bergs-shelton-2026-08-07"])
    assert m.match("atp-bergs-shelton-2026-08-07") == "atc-atp-bergs-shelton-2026-08-07"


def test_two_us_markets_with_one_signature_match_neither():
    # Moneyline and total under the same game slug. Picking one would fill the wrong
    # contract and then report it as the signal's own result.
    m = PMUSMatcher(["atc-mlb-bal-tex-2026-08-07-ml",
                     "atc-mlb-bal-tex-2026-08-07-ou"])
    assert m.match("mlb-bal-tex-2026-08-07") is None
    assert len(m.ambiguous) == 0   # the outcome token distinguishes them


def test_a_genuine_collision_refuses_instead_of_guessing():
    m = PMUSMatcher(["atc-mlb-bal-tex-2026-08-07", "atc-mlbtotals-bal-tex-2026-08-07"])
    assert m.match("mlb-bal-tex-2026-08-07") is None
    assert len(m.ambiguous) == 1


def test_the_same_slug_listed_twice_is_not_a_collision():
    m = PMUSMatcher(["atc-mlb-bal-tex-2026-08-07", "atc-mlb-bal-tex-2026-08-07"])
    assert m.match("mlb-bal-tex-2026-08-07") == "atc-mlb-bal-tex-2026-08-07"
