"""
Tests for OandaBroker.fetch_candles and OandaBroker.account_summary.

All tests use FakeClient — NO real network calls.
"""
from __future__ import annotations

import pytest

from forex_scalper.execution import AccountSummary, OandaBroker


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, req):
        self.requests.append(req)
        return self.responses.pop(0)


def _candles_resp():
    return {
        "candles": [
            {
                "time": "2025-01-06T13:00:00.000000000Z",
                "complete": True,
                "mid": {"o": "1.0850", "h": "1.0860", "l": "1.0840", "c": "1.0855"},
            },
            {
                "time": "2025-01-06T13:01:00.000000000Z",
                "complete": False,
                "mid": {"o": "1.0855", "h": "1.0858", "l": "1.0852", "c": "1.0857"},
            },
        ]
    }


def test_fetch_candles_maps_granularity_and_skips_incomplete():
    b = OandaBroker(account_id="X", token="T", client=FakeClient([_candles_resp()]))
    cs = b.fetch_candles("EUR_USD", "1M", count=2)
    assert len(cs) == 1                        # incomplete skipped
    assert cs[0].open == 1.0850 and cs[0].close == 1.0855
    # granularity param was M1
    assert b._client.requests[0].params["granularity"] == "M1"


def test_fetch_candles_unknown_tf_raises():
    b = OandaBroker(account_id="X", token="T", client=FakeClient([]))
    with pytest.raises(ValueError):
        b.fetch_candles("EUR_USD", "4H")


def test_account_summary_parses_currency_and_balance():
    resp = {
        "account": {
            "balance": "939.41",
            "NAV": "940.0",
            "currency": "SGD",
            "openTradeCount": 1,
        }
    }
    b = OandaBroker(account_id="X", token="T", client=FakeClient([resp]))
    s = b.account_summary()
    assert isinstance(s, AccountSummary) and s.currency == "SGD"
    assert s.balance == 939.41 and s.open_trade_count == 1


def test_fetch_candles_15m_granularity():
    b = OandaBroker(account_id="X", token="T", client=FakeClient([_candles_resp()]))
    b.fetch_candles("EUR_USD", "15M", count=2)
    assert b._client.requests[0].params["granularity"] == "M15"


def test_fetch_candles_1h_granularity():
    b = OandaBroker(account_id="X", token="T", client=FakeClient([_candles_resp()]))
    b.fetch_candles("EUR_USD", "1H", count=2)
    assert b._client.requests[0].params["granularity"] == "H1"


def test_account_summary_missing_open_trade_count_defaults_to_zero():
    resp = {
        "account": {
            "balance": "1000.00",
            "NAV": "1000.00",
            "currency": "USD",
        }
    }
    b = OandaBroker(account_id="X", token="T", client=FakeClient([resp]))
    s = b.account_summary()
    assert s.open_trade_count == 0


def test_closed_trade_pnl_returns_realized_for_closed():
    resp = {"trade": {"state": "CLOSED", "realizedPL": "7.50"}}
    b = OandaBroker(account_id="X", token="T", client=FakeClient([resp]))
    assert b.closed_trade_pnl("101") == 7.50


def test_closed_trade_pnl_none_when_open():
    resp = {"trade": {"state": "OPEN", "realizedPL": "0.0"}}
    b = OandaBroker(account_id="X", token="T", client=FakeClient([resp]))
    assert b.closed_trade_pnl("101") is None
