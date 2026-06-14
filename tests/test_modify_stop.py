"""Tests for broker.modify_stop capability (Task 2.3)."""


from forex_scalper.execution import OandaBroker, PaperBroker


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, req):
        self.requests.append(req)
        return self.responses.pop(0)


def test_paper_broker_modify_stop_updates_open_trade():
    pb = PaperBroker(pip_value={"EUR_USD": 0.00013})
    pb.set_price("EUR_USD", 1.0849, 1.0851)
    t = pb.place_market_order("EUR_USD", 1000, 1.0838, 1.0890, "A")
    ok = pb.modify_stop(t.trade_id, 1.0845)
    assert ok is True
    assert pb.open_trades()[0].stop_price == 1.0845


def test_paper_broker_modify_stop_unknown_trade_returns_false():
    pb = PaperBroker(pip_value={"EUR_USD": 0.00013})
    assert pb.modify_stop("nope", 1.0) is False


def test_oanda_modify_stop_sends_trade_crcdo_with_formatted_price():
    b = OandaBroker(
        account_id="X",
        token="T",
        practice=True,
        client=FakeClient([{"stopLossOrderTransaction": {"id": "999"}}]),
    )
    ok = b.modify_stop("101", 1.08450, instrument="EUR_USD")
    assert ok is True
    req = b._client.requests[0]
    # TradeCRCDO carries the new stopLoss price in its data body
    body = req.data
    assert body["stopLoss"]["price"] == "1.08450"


def test_oanda_modify_stop_jpy_three_decimals():
    b = OandaBroker(
        account_id="X",
        token="T",
        practice=True,
        client=FakeClient([{"stopLossOrderTransaction": {"id": "1"}}]),
    )
    b.modify_stop("77", 156.123, instrument="USD_JPY")
    assert b._client.requests[0].data["stopLoss"]["price"] == "156.123"
