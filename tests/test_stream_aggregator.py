"""
Tests for stream.py — TickAggregator, CandleResampler, MultiInstrumentStream.

All tests are PURE: no real network, no real OANDA client.
MultiInstrumentStream tests use a FakeClient (client= seam) that returns
a plain list of PRICE-message dicts; _stream_once() is driven directly so
there is no reconnect loop to deal with.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from forex_scalper.models import Candle
from forex_scalper.stream import (
    CandleResampler,
    MultiInstrumentStream,
    StreamEvent,
    StreamReconnectExhausted,
    TickAggregator,
    _bucket_start,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dt(h: int, m: int, s: int = 0, us: int = 0) -> datetime:
    """Return a UTC datetime on an arbitrary fixed date."""
    return datetime(2025, 1, 6, h, m, s, us, tzinfo=UTC)


def _price_msg(
    instrument: str,
    time_str: str,
    bid: str,
    ask: str,
) -> dict:
    return {
        "type": "PRICE",
        "instrument": instrument,
        "time": time_str,
        "bids": [{"price": bid}],
        "asks": [{"price": ask}],
    }


class FakeClient:
    """Fake oandapyV20.API that serves a fixed list of messages from request()."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages

    def request(self, req: object) -> list[dict]:
        return self._messages


# ---------------------------------------------------------------------------
# _bucket_start
# ---------------------------------------------------------------------------

class TestBucketStart:
    def test_1m_floors_to_minute(self):
        at = _dt(13, 30, 45, 123456)
        assert _bucket_start(at, 1) == _dt(13, 30)

    def test_15m_floors_to_quarter_hour(self):
        at = _dt(13, 37)
        assert _bucket_start(at, 15) == _dt(13, 30)

    def test_15m_exactly_on_boundary(self):
        at = _dt(13, 45)
        assert _bucket_start(at, 15) == _dt(13, 45)

    def test_60m_floors_to_hour(self):
        at = _dt(13, 59, 59)
        assert _bucket_start(at, 60) == _dt(13, 0)

    def test_60m_on_boundary(self):
        at = _dt(14, 0)
        assert _bucket_start(at, 60) == _dt(14, 0)


# ---------------------------------------------------------------------------
# TickAggregator
# ---------------------------------------------------------------------------

class TestTickAggregator:
    def test_first_tick_returns_none(self):
        agg = TickAggregator("EUR_USD", 1)
        result = agg.add_tick(1.0850, _dt(13, 30, 5))
        assert result is None

    def test_second_tick_same_minute_returns_none(self):
        agg = TickAggregator("EUR_USD", 1)
        agg.add_tick(1.0850, _dt(13, 30, 5))
        result = agg.add_tick(1.0855, _dt(13, 30, 45))
        assert result is None

    def test_tick_accumulates_high_and_low(self):
        agg = TickAggregator("EUR_USD", 1)
        agg.add_tick(1.0850, _dt(13, 30, 0))
        agg.add_tick(1.0860, _dt(13, 30, 20))  # new high
        agg.add_tick(1.0840, _dt(13, 30, 40))  # new low
        # force emission by crossing into next minute
        candle = agg.add_tick(1.0855, _dt(13, 31, 0))
        assert candle is not None
        assert candle.high == 1.0860
        assert candle.low == 1.0840

    def test_boundary_crossing_emits_completed_candle(self):
        agg = TickAggregator("EUR_USD", 1)
        agg.add_tick(1.0850, _dt(13, 30, 5))   # open
        agg.add_tick(1.0870, _dt(13, 30, 20))  # high
        agg.add_tick(1.0830, _dt(13, 30, 40))  # low
        agg.add_tick(1.0845, _dt(13, 30, 55))  # close (last in bucket)
        candle = agg.add_tick(1.0860, _dt(13, 31, 3))  # new bucket triggers emission
        assert candle is not None
        assert isinstance(candle, Candle)
        assert candle.time == _dt(13, 30)
        assert candle.open == 1.0850
        assert candle.high == 1.0870
        assert candle.low == 1.0830
        assert candle.close == 1.0845
        assert candle.complete is True

    def test_reset_discards_partial_bucket(self):
        agg = TickAggregator("EUR_USD", 1)
        agg.add_tick(1.0850, _dt(13, 30, 5))
        agg.reset()
        # After reset, the first tick is treated as a fresh start
        result = agg.add_tick(1.0860, _dt(13, 30, 10))
        assert result is None  # started fresh, no completed candle

    def test_reset_prevents_stale_candle_after_gap(self):
        agg = TickAggregator("EUR_USD", 1)
        agg.add_tick(1.0850, _dt(13, 30, 5))
        agg.reset()
        # Tick in a completely different minute after reset should not emit
        agg.add_tick(1.0860, _dt(13, 45, 0))
        result = agg.add_tick(1.0865, _dt(13, 46, 0))
        assert result is not None
        assert result.time == _dt(13, 45)

    def test_close_is_last_tick_in_bucket(self):
        agg = TickAggregator("EUR_USD", 1)
        agg.add_tick(1.0850, _dt(13, 30, 0))
        agg.add_tick(1.0860, _dt(13, 30, 30))
        agg.add_tick(1.0845, _dt(13, 30, 59))  # this should be the close
        candle = agg.add_tick(1.0870, _dt(13, 31, 0))
        assert candle is not None
        assert candle.close == 1.0845  # last tick before boundary

    def test_open_is_first_tick_in_bucket(self):
        agg = TickAggregator("EUR_USD", 1)
        agg.add_tick(1.0850, _dt(13, 30, 0))   # open of bucket
        agg.add_tick(1.0870, _dt(13, 30, 30))
        candle = agg.add_tick(1.0860, _dt(13, 31, 0))
        assert candle is not None
        assert candle.open == 1.0850


# ---------------------------------------------------------------------------
# CandleResampler — 15M
# ---------------------------------------------------------------------------

def _make_candle(h: int, m: int, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(time=_dt(h, m), open=open_, high=high, low=low, close=close, complete=True)


class TestCandleResampler15M:
    def test_first_candle_returns_none(self):
        rs = CandleResampler(15)
        result = rs.add(_make_candle(13, 0, 1.08, 1.09, 1.07, 1.085))
        assert result is None

    def test_within_same_bucket_returns_none(self):
        rs = CandleResampler(15)
        rs.add(_make_candle(13, 0, 1.08, 1.09, 1.07, 1.085))
        result = rs.add(_make_candle(13, 1, 1.085, 1.092, 1.078, 1.090))
        assert result is None

    def test_15_candles_then_boundary_emits_15m_candle(self):
        rs = CandleResampler(15)
        # Feed 15 x 1M candles: 13:00 through 13:14
        candles = [
            _make_candle(13, m, open_=1.0800 + m * 0.0001, high=1.0810 + m * 0.0001,
                         low=1.0790 + m * 0.0001, close=1.0805 + m * 0.0001)
            for m in range(15)
        ]
        for c in candles:
            result = rs.add(c)
            assert result is None, f"Expected None for candle at 13:{c.time.minute:02d}"

        # 16th candle at 13:15 triggers emission of the 13:00 bucket
        trigger = _make_candle(13, 15, 1.0900, 1.0920, 1.0880, 1.0910)
        emitted = rs.add(trigger)
        assert emitted is not None
        assert isinstance(emitted, Candle)
        assert emitted.time == _dt(13, 0)
        assert emitted.open == candles[0].open
        assert emitted.high == max(c.high for c in candles)
        assert emitted.low == min(c.low for c in candles)
        assert emitted.close == candles[-1].close  # last candle before boundary
        assert emitted.complete is True

    def test_reset_discards_partial_bucket(self):
        rs = CandleResampler(15)
        rs.add(_make_candle(13, 0, 1.08, 1.09, 1.07, 1.085))
        rs.add(_make_candle(13, 1, 1.085, 1.092, 1.078, 1.090))
        rs.reset()
        # After reset, feeding a new boundary candle should start fresh
        result = rs.add(_make_candle(13, 15, 1.09, 1.10, 1.08, 1.095))
        assert result is None  # fresh start, no completed bucket

    def test_high_low_correctly_tracked_across_bucket(self):
        rs = CandleResampler(15)
        # Feed candles where high/low vary throughout the 15-minute window
        rs.add(_make_candle(13, 0, 1.0850, 1.0900, 1.0840, 1.0870))
        rs.add(_make_candle(13, 1, 1.0870, 1.0950, 1.0860, 1.0920))  # global high
        rs.add(_make_candle(13, 2, 1.0920, 1.0930, 1.0800, 1.0850))  # global low
        trigger = _make_candle(13, 15, 1.085, 1.086, 1.084, 1.085)
        emitted = rs.add(trigger)
        assert emitted is not None
        assert emitted.high == 1.0950
        assert emitted.low == 1.0800


# ---------------------------------------------------------------------------
# CandleResampler — 1H
# ---------------------------------------------------------------------------

class TestCandleResampler1H:
    def test_hour_boundary_emits_1h_candle(self):
        rs = CandleResampler(60)
        # Feed 1M candles: 13:00 through 13:59
        candles = [
            _make_candle(13, m, open_=1.0800, high=1.0900 + m * 0.0001,
                         low=1.0700 - m * 0.0001, close=1.0800 + m * 0.0001)
            for m in range(60)
        ]
        for c in candles:
            rs.add(c)

        # Candle at 14:00 triggers emission of the 13:00 bucket
        trigger = _make_candle(14, 0, 1.0850, 1.0860, 1.0840, 1.0855)
        emitted = rs.add(trigger)
        assert emitted is not None
        assert emitted.time == _dt(13, 0)
        assert emitted.open == candles[0].open
        assert emitted.high == max(c.high for c in candles)
        assert emitted.low == min(c.low for c in candles)
        assert emitted.close == candles[-1].close
        assert emitted.complete is True

    def test_within_hour_returns_none(self):
        rs = CandleResampler(60)
        rs.add(_make_candle(13, 0, 1.08, 1.09, 1.07, 1.085))
        result = rs.add(_make_candle(13, 30, 1.085, 1.092, 1.078, 1.090))
        assert result is None


# ---------------------------------------------------------------------------
# MultiInstrumentStream — routing test via FakeClient
# ---------------------------------------------------------------------------

class TestMultiInstrumentStream:
    """Drive _stream_once() directly with a FakeClient — no network, no reconnect loop."""

    def _make_stream(self, messages: list[dict]) -> MultiInstrumentStream:
        return MultiInstrumentStream(
            token="T",
            account_id="X",
            instruments=["EUR_USD", "USD_JPY"],
            client=FakeClient(messages),
        )

    def _msgs_spanning_boundary(self) -> list[dict]:
        """Two instruments, ticks spanning 13:30→13:31 so each emits one 1M candle."""
        return [
            # Heartbeat — must be skipped
            {"type": "HEARTBEAT", "time": "2025-01-06T13:30:00.000000Z"},
            # EUR_USD ticks in 13:30 bucket
            _price_msg("EUR_USD", "2025-01-06T13:30:10.000000Z", "1.08490", "1.08510"),
            _price_msg("EUR_USD", "2025-01-06T13:30:50.000000Z", "1.08480", "1.08500"),
            # USD_JPY ticks in 13:30 bucket
            _price_msg("USD_JPY", "2025-01-06T13:30:15.000000Z", "156.300", "156.320"),
            _price_msg("USD_JPY", "2025-01-06T13:30:45.000000Z", "156.350", "156.370"),
            # EUR_USD tick in 13:31 → triggers EUR_USD candle for 13:30
            _price_msg("EUR_USD", "2025-01-06T13:31:05.000000Z", "1.08520", "1.08540"),
            # USD_JPY tick in 13:31 → triggers USD_JPY candle for 13:30
            _price_msg("USD_JPY", "2025-01-06T13:31:10.000000Z", "156.280", "156.300"),
        ]

    def test_heartbeat_is_skipped(self):
        msgs = self._msgs_spanning_boundary()
        stream = self._make_stream(msgs)
        events = list(stream._stream_once())
        # Only 2 events (one per instrument), heartbeat must not appear
        assert len(events) == 2

    def test_emits_stream_event_for_each_instrument(self):
        msgs = self._msgs_spanning_boundary()
        stream = self._make_stream(msgs)
        events = list(stream._stream_once())
        instruments = {e.instrument for e in events}
        assert "EUR_USD" in instruments
        assert "USD_JPY" in instruments

    def test_eur_usd_candle_correct_ohlc(self):
        msgs = self._msgs_spanning_boundary()
        stream = self._make_stream(msgs)
        events = {e.instrument: e for e in stream._stream_once()}
        eu = events["EUR_USD"]
        assert isinstance(eu, StreamEvent)
        assert isinstance(eu.candle, Candle)
        # bucket start = 13:30:00 UTC
        assert eu.candle.time == datetime(2025, 1, 6, 13, 30, 0, tzinfo=UTC)
        # open = mid of first tick = (1.08490 + 1.08510) / 2 = 1.08500
        assert abs(eu.candle.open - 1.08500) < 1e-9
        # close = mid of second 13:30 tick = (1.08480 + 1.08500) / 2 = 1.08490
        assert abs(eu.candle.close - 1.08490) < 1e-9
        assert eu.candle.high >= eu.candle.low
        assert eu.candle.complete is True

    def test_usd_jpy_candle_correct_bucket_time(self):
        msgs = self._msgs_spanning_boundary()
        stream = self._make_stream(msgs)
        events = {e.instrument: e for e in stream._stream_once()}
        jpy = events["USD_JPY"]
        assert jpy.candle.time == datetime(2025, 1, 6, 13, 30, 0, tzinfo=UTC)

    def test_stream_event_carries_latest_bid_ask(self):
        msgs = self._msgs_spanning_boundary()
        stream = self._make_stream(msgs)
        events = {e.instrument: e for e in stream._stream_once()}
        eu = events["EUR_USD"]
        # The last EUR_USD tick in 13:30 had bid=1.08480, ask=1.08500
        # (the 13:31 tick that triggers emission has bid=1.08520, ask=1.08540,
        #  but the StreamEvent carries the bid/ask AT THE MOMENT OF EMISSION,
        #  which is the triggering tick's bid/ask)
        # Per the spec: "bid/ask are the latest tick's bid/ask for that instrument"
        assert abs(eu.bid - 1.08520) < 1e-9
        assert abs(eu.ask - 1.08540) < 1e-9

    def test_non_price_message_skipped(self):
        """Extra non-PRICE message types (besides HEARTBEAT) are also skipped."""
        msgs = [
            {"type": "TRANSACTION", "time": "2025-01-06T13:30:00Z"},
            _price_msg("EUR_USD", "2025-01-06T13:30:10.000000Z", "1.08490", "1.08510"),
            _price_msg("EUR_USD", "2025-01-06T13:31:00.000000Z", "1.08500", "1.08520"),
        ]
        stream = MultiInstrumentStream(
            token="T",
            account_id="X",
            instruments=["EUR_USD"],
            client=FakeClient(msgs),
        )
        events = list(stream._stream_once())
        assert len(events) == 1
        assert events[0].instrument == "EUR_USD"

    def test_unknown_instrument_skipped(self):
        """Messages for instruments not in the instruments list are silently ignored."""
        msgs = [
            _price_msg("GBP_USD", "2025-01-06T13:30:10.000000Z", "1.26000", "1.26020"),
            _price_msg("EUR_USD", "2025-01-06T13:30:15.000000Z", "1.08490", "1.08510"),
            _price_msg("EUR_USD", "2025-01-06T13:31:00.000000Z", "1.08500", "1.08520"),
        ]
        stream = MultiInstrumentStream(
            token="T",
            account_id="X",
            instruments=["EUR_USD"],  # GBP_USD NOT listed
            client=FakeClient(msgs),
        )
        events = list(stream._stream_once())
        assert len(events) == 1
        assert events[0].instrument == "EUR_USD"

    def test_aggregators_reset_on_each_stream_once_call(self):
        """Calling _stream_once twice must reset aggregators so the second call
        does not inherit stale partial state from the first."""
        tick_t1 = _price_msg("EUR_USD", "2025-01-06T13:30:10.000000Z", "1.08490", "1.08510")
        # First _stream_once: one tick, no boundary crossed → no events
        stream = MultiInstrumentStream(
            token="T",
            account_id="X",
            instruments=["EUR_USD"],
            client=FakeClient([tick_t1]),
        )
        events1 = list(stream._stream_once())
        assert events1 == []

        # Second _stream_once: aggregators should be reset; one tick again → no events
        stream._client = FakeClient([tick_t1])
        events2 = list(stream._stream_once())
        assert events2 == []  # no stale partial from call 1


# ---------------------------------------------------------------------------
# StreamReconnectExhausted
# ---------------------------------------------------------------------------

class TestStreamReconnectExhausted:
    def test_is_exception_subclass(self):
        exc = StreamReconnectExhausted("test")
        assert isinstance(exc, Exception)
        assert str(exc) == "test"

    def test_raised_after_max_failures(self):
        """iter_events raises StreamReconnectExhausted after too many request failures."""
        import requests

        class AlwaysFailClient:
            def request(self, req: object) -> list[dict]:
                raise requests.exceptions.ConnectionError("boom")

        stream = MultiInstrumentStream(
            token="T",
            account_id="X",
            instruments=["EUR_USD"],
            client=AlwaysFailClient(),
            now=iter([0.0, 1.0, 2.0, 3.0, 4.0]).__next__,
            sleep=lambda s: None,
        )
        with pytest.raises(StreamReconnectExhausted):
            list(stream.iter_events())
