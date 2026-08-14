"""The de-vig A/B trail: fair_mult / fair_power / fair_shin on every sports record.

The point of carrying all three fair values on the operative ticket is that the
paper trial can later score which method would have done better WITHOUT running a
second trial. These tests pin each hop of that trail: scanner -> ScanResult,
ScanResult -> paper ledger row, and board ticket -> papertrial row (the same
MODEL_FIELDS pattern the weather sleeve uses for forecast_src etc.).
"""

import json
import logging
import os
import tempfile
from dataclasses import replace

from predictionedge import papertrial
from predictionedge.config import Config
from predictionedge.edge import Opportunity
from predictionedge.kalshi import MockKalshiClient
from predictionedge.ledger import PaperLedger
from predictionedge.odds import MockOddsProvider
from predictionedge.scanner import find_opportunities
from predictionedge.whales import NeutralWhaleProvider

LOG = logging.getLogger("pe-test")
LOG.addHandler(logging.NullHandler())

AB_FIELDS = ("fair_mult", "fair_power", "fair_shin")


def _opp(**over):
    base = dict(ticker="KXNBA-LALBOS", side="yes", fair_prob=0.54, price=0.46,
                edge_per_contract=0.05, contracts=10, stake=4.6, est_fee=0.05,
                expected_value=0.5)
    return Opportunity(**{**base, **over})


def test_scanner_results_carry_all_three_fair_values():
    cfg = Config()
    results = find_opportunities(cfg, MockOddsProvider(), MockKalshiClient(),
                                 NeutralWhaleProvider())
    assert results, "mock data is built to produce at least the Lakers edge"
    for r in results:
        for field in AB_FIELDS:
            assert 0.0 < r.fair_all[field] < 1.0, field
        # The operative method is stamped on the record, so a later reader knows
        # which of the three numbers the position was actually taken on.
        assert r.fair_all["devig_method"] == cfg.devig_method
        # Direction sanity on the mock Lakers book (favourite YES side): power
        # and shin sit at or above multiplicative.
        assert r.fair_all["fair_power"] >= r.fair_all["fair_mult"] - 1e-4


def test_scanner_default_method_is_power():
    assert Config().devig_method == "power"


def test_ledger_records_extra_fields_verbatim():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "ledger.jsonl")
    ledger = PaperLedger(path)
    ledger.record(_opp(), note="sports",
                  extra={"fair_mult": 0.9048, "fair_power": 0.9393,
                         "fair_shin": 0.925, "devig_method": "power"})
    row = json.loads(open(path, encoding="utf-8").read().splitlines()[0])
    assert row["fair_mult"] == 0.9048
    assert row["fair_power"] == 0.9393
    assert row["fair_shin"] == 0.925
    assert row["devig_method"] == "power"


def test_ledger_extra_cannot_overwrite_the_accounting():
    """Metadata annotates a row; it must never falsify stake/price/status."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "ledger.jsonl")
    ledger = PaperLedger(path)
    ledger.record(_opp(price=0.46), note="sports",
                  extra={"price": 0.01, "status": "resolved", "fair_shin": 0.9})
    row = json.loads(open(path, encoding="utf-8").read().splitlines()[0])
    assert row["price"] == 0.46
    assert row["status"] == "open"
    assert row["fair_shin"] == 0.9


def test_papertrial_carries_devig_ab_fields_onto_the_row():
    """Same pattern as the weather sleeve's forecast_src: ticket -> trial row."""
    trial = {"open": [], "settled": [], "stats": {}}
    ticket = {"market_id": "KXNBAGAME-26AUG13-LALBOS", "outcome": "Yes",
              "title": "Lakers beat Celtics", "entry_price": 0.46,
              "source": "sports", "fair_mult": 0.9048, "fair_power": 0.9393,
              "fair_shin": 0.925, "devig_method": "power"}
    assert papertrial.record(trial, [ticket], now=1.0) == 1
    row = trial["open"][0]
    assert row["fair_mult"] == 0.9048
    assert row["fair_power"] == 0.9393
    assert row["fair_shin"] == 0.925
    assert row["devig_method"] == "power"


def test_scanner_fair_all_respects_operative_method_override():
    """Switching the operative method changes the stamp, not the A/B fields."""
    cfg = replace(Config(), devig_method="shin")
    results = find_opportunities(cfg, MockOddsProvider(), MockKalshiClient(),
                                 NeutralWhaleProvider())
    assert results
    for r in results:
        assert r.fair_all["devig_method"] == "shin"
        assert set(AB_FIELDS) <= set(r.fair_all)
