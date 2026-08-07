"""The advisory sections of the published payload.

These sources (E1 consistency, E3 macro) are deliberately NOT tickets, and the thing
most worth pinning is what they do when they fail: an outage must publish "unknown",
never an empty result that renders as a clean book. That is the market_meta lesson,
and it is the only part of this plumbing that can lose money by rotting quietly.
"""

from __future__ import annotations

import pytest

from predictionedge import dashboard, publish
from predictionedge.config import Config
from predictionedge.consistency import Leg, ScanReport, Violation
from predictionedge.macrofv import (Bracket, BracketCompare, CpiComparison, CpiMatch,
                                    Nowcast)


def _leg(name="Above 100k", bid=0.40, ask=0.42):
    return Leg(condition_id="0x" + "a" * 64, event_slug="btc-eoy", question=f"Will BTC be {name}?",
               name=name, bid=bid, ask=ask, liquidity=50_000.0, end_iso="2026-12-31T00:00:00Z")


def _violation():
    return Violation(family="ladder", event_slug="btc-eoy",
                     constraint="P(BTC > 120k) must not exceed P(BTC > 100k)",
                     edge_c=3.5, legs=(_leg("Above 100k"), _leg("Above 120k", 0.45, 0.47)),
                     actions=("buy Above 100k at 0.42",), warnings=("thin book",))


def _comparison(note=""):
    b = Bracket(label="3.0% or more", lo=3.0, hi=float("inf"), market_prob=0.20,
                question="CPI 3.0% or more?", condition_id="0x" + "b" * 64, volume=1000.0)
    return CpiComparison(
        match=CpiMatch(slug="cpi-aug", title="August CPI", series="cpi", basis="yoy",
                       ref_month="2026-8", brackets=[b]),
        nowcast=Nowcast(series="cpi", basis="yoy", ref_month="2026-8", value_pp=2.85,
                        as_of="2026-08-06", printed=False),
        sigma=0.16,
        rows=[BracketCompare(bracket=b, model_prob=0.18, gap_c=-2.0, suspect=False)],
        model_sigma_note=note)


# --- consistency -----------------------------------------------------------

def test_consistency_serialises_violation(monkeypatch):
    rep = ScanReport(violations=[_violation()], events_scanned=42,
                     rejected={"unpriceable leg": 3}, notes=["heads up"])
    monkeypatch.setattr("predictionedge.consistency.scan", lambda cfg: rep)
    out = dashboard._consistency_section(Config())

    assert out["ok"] is True and out["events_scanned"] == 42
    v = out["violations"][0]
    assert v["family"] == "ladder" and v["edge_c"] == 3.5
    assert len(v["legs"]) == 2 and v["legs"][0]["ask"] == 0.42
    assert v["warnings"] == ["thin book"] and v["url"].endswith("btc-eoy")
    assert out["rejected"] == {"unpriceable leg": 3}


def test_consistency_failure_is_unknown_not_clean(monkeypatch):
    def boom(cfg):
        raise RuntimeError("gamma down")
    monkeypatch.setattr("predictionedge.consistency.scan", boom)
    out = dashboard._consistency_section(Config())

    # The whole point: ok=False. An empty violations list ALONE would render as
    # "the book is coherent", which is the opposite of what we know.
    assert out["ok"] is False
    assert out["violations"] == []
    assert "gamma down" in out["notes"][0]


def test_consistency_disabled_is_ok_but_flagged():
    out = dashboard._consistency_section(Config(consistency_enabled=False))
    assert out["ok"] is True and out["enabled"] is False


# --- macro -----------------------------------------------------------------

def test_macro_serialises_comparison(monkeypatch):
    monkeypatch.setattr("predictionedge.macrofv.run_cpi", lambda cfg: [_comparison()])
    out = dashboard._macro_section(Config())

    assert out["ok"] is True
    c = out["cpi"][0]
    assert c["nowcast_pp"] == 2.85 and c["ref_month"] == "2026-8"
    assert c["rows"][0]["gap_c"] == -2.0 and c["rows"][0]["suspect"] is False


def test_macro_carries_variance_note_verbatim(monkeypatch):
    note = "VARIANCE DISAGREEMENT: model sigma 0.160pp vs market-implied 0.100pp"
    monkeypatch.setattr("predictionedge.macrofv.run_cpi", lambda cfg: [_comparison(note)])
    out = dashboard._macro_section(Config())
    # Dropping this note turns one disagreement about width into a column of fake edges.
    assert out["cpi"][0]["model_sigma_note"] == note


def test_macro_failure_is_unknown_not_clean(monkeypatch):
    def boom(cfg):
        raise RuntimeError("cleveland fed 503")
    monkeypatch.setattr("predictionedge.macrofv.run_cpi", boom)
    out = dashboard._macro_section(Config())
    assert out["ok"] is False and out["cpi"] == []
    assert "cleveland fed 503" in out["notes"][0]


def test_macro_disabled_is_ok_but_flagged():
    out = dashboard._macro_section(Config(macro_enabled=False))
    assert out["ok"] is True and out["enabled"] is False


# --- payload wiring --------------------------------------------------------

def test_advisory_sections_never_become_tickets(monkeypatch):
    """A violation must not leak into the list the board offers to place."""
    monkeypatch.setattr("predictionedge.consistency.scan",
                        lambda cfg: ScanReport(violations=[_violation()]))
    monkeypatch.setattr("predictionedge.macrofv.run_cpi", lambda cfg: [_comparison()])
    monkeypatch.setattr(dashboard, "build_whale_provider", lambda cfg, **kw: object())

    payload = dashboard.build_board_payload(Config())
    assert payload["tickets"] == []
    assert len(payload["consistency"]["violations"]) == 1
    assert len(payload["macro"]["cpi"]) == 1


def test_mock_mode_stays_offline(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("mock mode must not hit the network")
    monkeypatch.setattr("predictionedge.consistency.scan", boom)
    monkeypatch.setattr("predictionedge.macrofv.run_cpi", boom)
    monkeypatch.setattr(dashboard, "build_whale_provider", lambda cfg, **kw: object())

    payload = dashboard.build_board_payload(Config(), force_mock=True)
    assert payload["consistency"]["violations"] == []
    assert payload["macro"]["cpi"] == []


def test_publish_fallback_marks_sections_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(publish, "build_payload",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("nope")))
    out = tmp_path / "board.json"
    assert publish.main(["--out", str(out)]) == 0

    import json
    data = json.loads(out.read_text(encoding="utf-8"))
    # A total board failure must not publish a page reading "no violations found".
    assert data["consistency"]["ok"] is False
    assert data["macro"]["ok"] is False
