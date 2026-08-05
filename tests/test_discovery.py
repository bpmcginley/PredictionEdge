from dataclasses import replace

from predictionedge.config import Config
from predictionedge.discovery import discover_links
from predictionedge.kalshi import KalshiMarket
from predictionedge.odds import MockOddsProvider

EVENTS = MockOddsProvider().events()


class FakeKalshiList:
    def __init__(self, markets):
        self._m = markets

    def list_markets(self, *, series_ticker=None, status="open", **kw):
        if series_ticker:
            return [m for m in self._m if m.ticker.startswith(series_ticker)]
        return list(self._m)


def test_discover_links_matches_subtitle_markets():
    cfg = replace(Config(), dynamic_discovery=True, sports_series=("KXNBAGAME",))
    markets = [KalshiMarket("KXNBAGAME-26X-LAL", "Lakers vs Celtics Winner?",
                            0.5, 0.52, 0.48, 0.5,
                            yes_sub_title="Lakers", no_sub_title="Celtics")]
    links = discover_links(cfg, FakeKalshiList(markets), EVENTS)
    assert len(links) == 1
    assert links[0].event_id == "evt-lakers-celtics"
    assert links[0].yes_is_outcome0 is True


def test_discover_links_off_by_default():
    cfg = Config()  # dynamic_discovery defaults False
    links = discover_links(cfg, FakeKalshiList([]), EVENTS)
    assert links == []
