"""
Tests for warmup.warm_up — preloading MarketState with historical candles.
"""
from __future__ import annotations

from datetime import UTC, datetime

from forex_scalper.data import MarketState
from forex_scalper.models import Candle
from forex_scalper.warmup import warm_up


def _c(m):
    return Candle(datetime(2025, 1, 1, minute=m % 60, tzinfo=UTC), 1, 1, 1, 1)


class FakeBroker:
    def __init__(self):
        self.calls = []

    def fetch_candles(self, instrument, tf, count):
        self.calls.append((instrument, tf, count))
        return [_c(0), _c(1), _c(2)]


def test_warmup_preloads_all_timeframes():
    m = MarketState()
    fb = FakeBroker()
    warm_up(m, fb, ["EUR_USD"], counts={"1H": 3, "15M": 3, "1M": 3})
    assert len(m.candles("EUR_USD", "1H")) == 3
    assert len(m.candles("EUR_USD", "15M")) == 3
    assert len(m.candles("EUR_USD", "1M")) == 3


def test_warmup_skips_failing_timeframe():
    m = MarketState()

    class Flaky:
        def fetch_candles(self, instrument, tf, count):
            if tf == "15M":
                raise RuntimeError("boom")
            return [Candle(datetime(2025, 1, 1, tzinfo=UTC), 1, 1, 1, 1)]

    warm_up(m, Flaky(), ["EUR_USD"], counts={"1H": 1, "15M": 1, "1M": 1})
    assert len(m.candles("EUR_USD", "1H")) == 1
    assert len(m.candles("EUR_USD", "15M")) == 0   # failed, skipped
    assert len(m.candles("EUR_USD", "1M")) == 1


def test_warmup_uses_default_counts_when_none_provided():
    m = MarketState()
    fb = FakeBroker()
    warm_up(m, fb, ["EUR_USD"])
    # Default counts: 1H=120, 15M=200, 1M=200
    calls = {tf: cnt for (_, tf, cnt) in fb.calls}
    assert calls["1H"] == 120
    assert calls["15M"] == 200
    assert calls["1M"] == 200


def test_warmup_loads_1h_and_15m_before_1m():
    """Verify timeframe ordering: 1H then 15M then 1M per instrument."""
    m = MarketState()
    fb = FakeBroker()
    warm_up(m, fb, ["EUR_USD"], counts={"1H": 1, "15M": 1, "1M": 1})
    tfs = [tf for (_, tf, _) in fb.calls]
    assert tfs == ["1H", "15M", "1M"]


def test_warmup_multiple_instruments():
    m = MarketState()
    fb = FakeBroker()
    warm_up(m, fb, ["EUR_USD", "GBP_USD"], counts={"1H": 2, "15M": 2, "1M": 2})
    assert len(m.candles("EUR_USD", "1M")) == 3
    assert len(m.candles("GBP_USD", "1M")) == 3
    assert len(fb.calls) == 6  # 3 timeframes x 2 instruments
