"""
Tests for the live OandaBroker adapter.

All tests use FakeClient — NO real network calls.
The `client=` seam on OandaBroker.__init__ injects the fake without triggering
real oandapyV20.API / truststore / network setup.
"""
from __future__ import annotations

import logging

import pytest
import requests
from oandapyV20.exceptions import V20Error

from forex_scalper.execution import OandaBroker, OpenTrade


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, req):
        self.requests.append(req)
        return self.responses.pop(0)


def _fill(trade_id="101", price="1.08500", time="2025-01-06T13:30:00.000000Z"):
    return {
        "orderFillTransaction": {
            "price": price,
            "time": time,
            "tradeOpened": {"tradeID": trade_id},
        }
    }


def _broker(responses):
    return OandaBroker(
        account_id="X", token="T", practice=True, client=FakeClient(responses)
    )


def test_place_long_sends_market_fok_with_sl_tp_and_idempotency_id():
    b = _broker([_fill()])
    t = b.place_market_order("EUR_USD", 1000, 1.0838, 1.0890, setup="A")
    body = b._client.requests[0].data["order"]
    assert body["type"] == "MARKET" and body["timeInForce"] == "FOK"
    assert body["units"] == "1000"                        # signed, long
    assert body["stopLossOnFill"]["price"] == "1.08380"   # 5dp non-JPY
    assert body["takeProfitOnFill"]["price"] == "1.08900"
    assert body["clientExtensions"]["id"]                 # idempotency key present, non-empty
    assert isinstance(t, OpenTrade) and t.trade_id == "101" and t.units == 1000
    assert t.entry_price == 1.0850 and t.stop_price == 1.0838 and t.take_profit == 1.0890


def test_place_short_sends_negative_units():
    b = _broker([_fill(trade_id="55")])
    t = b.place_market_order("EUR_USD", -2000, 1.0890, 1.0820, setup="A")
    assert b._client.requests[0].data["order"]["units"] == "-2000"
    assert t.units == -2000


def test_jpy_prices_formatted_to_three_decimals():
    b = _broker([_fill(price="156.400")])
    b.place_market_order("USD_JPY", 1000, 156.10, 156.90, setup="B")
    body = b._client.requests[0].data["order"]
    assert body["stopLossOnFill"]["price"] == "156.100"
    assert body["takeProfitOnFill"]["price"] == "156.900"


def test_rejected_order_returns_none():
    b = _broker([{"orderRejectTransaction": {"rejectReason": "MARKET_HALTED"}}])
    assert b.place_market_order("EUR_USD", 1000, 1.0838, 1.0890) is None


def test_close_trade_returns_realized_pnl():
    b = _broker(
        [
            {
                "orderFillTransaction": {
                    "price": "1.0900",
                    "time": "2025-01-06T14:00:00.000000Z",
                    "tradesClosed": [{"realizedPL": "12.34"}],
                }
            }
        ]
    )
    assert b.close_trade("101") == 12.34


def test_open_trades_maps_signed_units_and_sl_tp():
    resp = {
        "trades": [
            {
                "id": "101",
                "instrument": "EUR_USD",
                "currentUnits": "1000",
                "price": "1.0850",
                "openTime": "2025-01-06T13:30:00.000000Z",
                "stopLossOrder": {"price": "1.0838"},
                "takeProfitOrder": {"price": "1.0890"},
            },
            {
                "id": "102",
                "instrument": "USD_JPY",
                "currentUnits": "-3000",
                "price": "156.40",
                "openTime": "2025-01-06T13:31:00.000000Z",
            },
        ]
    }
    b = _broker([resp])
    ts = b.open_trades()
    assert ts[0].units == 1000 and ts[0].stop_price == 1.0838 and ts[0].take_profit == 1.0890
    assert ts[1].units == -3000   # short, signed
    assert ts[1].instrument == "USD_JPY"


def test_get_price_returns_bid_ask():
    resp = {"prices": [{"bids": [{"price": "1.08495"}], "asks": [{"price": "1.08505"}]}]}
    b = _broker([resp])
    assert b.get_price("EUR_USD") == (1.08495, 1.08505)


def test_close_trade_real_v20error_reraises():
    class ErrClient:
        def request(self, req):
            raise V20Error(400, "TRADE_REJECT")
    b = OandaBroker(account_id="X", token="T", practice=True, client=ErrClient())
    with pytest.raises(V20Error):
        b.close_trade("999")


def test_close_trade_404_returns_none(caplog):
    class GoneClient:
        def request(self, req):
            raise V20Error(404, "NO_SUCH_TRADE")
    b = OandaBroker(account_id="X", token="T", practice=True, client=GoneClient())
    with caplog.at_level(logging.INFO, logger="execution"):
        assert b.close_trade("999") is None
    assert "999" in caplog.text


def test_get_price_retries_then_succeeds():
    class FlakyClient:
        def __init__(self): self.n = 0
        def request(self, req):
            self.n += 1
            if self.n == 1:
                raise requests.ConnectionError("boom")
            return {"prices":[{"bids":[{"price":"1.10000"}],"asks":[{"price":"1.10010"}]}]}
    b = OandaBroker(account_id="X", token="T", practice=True, client=FlakyClient())
    b._sleeps = (0.0, 0.0, 0.0)   # no real sleeping in test
    assert b.get_price("EUR_USD") == (1.10000, 1.10010)
    assert b._client.n == 2
