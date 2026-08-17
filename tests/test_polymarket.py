"""Units of the public-data types, pinned where the mock can pin them.

`Trade.size` is a CONTRACT count and `MarketHolder.yes_size`/`no_size` are contract
counts too - the /trades and /holders endpoints both report tokens, and only /trades
carries the price that turns them into money. Reading either as dollars is the bug
that inflated every whale figure by 1/price, so the feed filter is pinned here in the
one place a test can see it: the mock everything else is tested against.
"""

from predictionedge.polymarket import MarketHolder, MockPolymarketDataClient, Trade


def _t(size, price, cid="0xA"):
    return Trade(wallet="0xW", name="", side="BUY", size=size, price=price, ts=1000,
                 title="Some market", outcome="Yes", condition_id=cid,
                 event_slug="evt", slug="mkt")


def test_the_mock_feed_filters_on_cash_like_the_live_api():
    """The live client asks for filterType=CASH, so the bar is size*price.

    Both fixtures are 20,000 contracts. At 30c that is $6,000 and clears a $5,000 bar;
    at 20c it is $4,000 and does not. A mock that compared the contract count would
    return both, and every test built on it would be blind to the units.
    """
    client = MockPolymarketDataClient(trades=[_t(20_000, 0.30, cid="0xRICH"),
                                              _t(20_000, 0.20, cid="0xTHIN")])
    got = client.recent_trades(min_usd=5_000)
    assert [t.condition_id for t in got] == ["0xRICH"]


def test_a_huge_contract_count_at_a_penny_price_is_not_a_big_trade():
    """500,000 contracts sounds enormous and is $5,000 at 1c - the exact confusion."""
    client = MockPolymarketDataClient(trades=[_t(500_000, 0.01)])
    assert client.recent_trades(min_usd=10_000) == []
    assert len(client.recent_trades(min_usd=5_000)) == 1


def test_the_feed_limit_applies_after_the_cash_filter():
    trades = [_t(30_000, 0.50, cid=f"0x{i}") for i in range(5)]
    trades.append(_t(30_000, 0.01, cid="0xTINY"))     # $300, never eligible
    got = MockPolymarketDataClient(trades=trades).recent_trades(min_usd=5_000, limit=3)
    assert len(got) == 3 and "0xTINY" not in [t.condition_id for t in got]


def test_holder_sizes_are_the_raw_token_count_not_a_dollar_conversion(monkeypatch):
    """/holders `amount` is a contract count and is passed through untouched.

    A position is not a fill: nothing on this endpoint says what the wallet paid, so
    there is no honest price here to multiply by. Cost basis lives on /positions as
    avgPrice/initialValue; today's quote would give mark-to-market instead, a different
    claim. If either ever gets folded in silently, this exact-equality check fails.

    The payload is a real one, trimmed: /holders reported amount 71704.105913 for this
    wallet, and /positions reported size 71704.1059, avgPrice 0.6274, initialValue
    44993.54 for the same wallet and market - the count matches, the dollars do not.
    """
    import sys

    payload = [{"token": "9582...146",
                "holders": [{"proxyWallet": "0xfe78", "amount": 71704.105913,
                             "outcomeIndex": 0}]},
               {"token": "7492...031",
                "holders": [{"proxyWallet": "0xdac3", "amount": 31770.811346,
                             "outcomeIndex": 1}]}]

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class _FakeRequests:
        @staticmethod
        def get(url, params=None, timeout=None):
            assert url.endswith("/holders")
            return _Resp()

    monkeypatch.setitem(sys.modules, "requests", _FakeRequests)
    from predictionedge.polymarket import LivePolymarketDataClient

    holders = LivePolymarketDataClient().market_holders("0xCID")
    assert holders == [MarketHolder("0xfe78", yes_size=71704.105913, no_size=0.0),
                       MarketHolder("0xdac3", yes_size=0.0, no_size=31770.811346)]
