"""stream.py
===========
Multi-instrument OANDA pricing tick-stream -> per-instrument 1-minute candles,
with online resampling to higher timeframes (15M, 1H). No lookahead.

Classes
-------
  _bucket_start          -- floor a datetime to its bucket boundary
  TickAggregator         -- accumulates raw ticks -> completed 1M Candles
  CandleResampler        -- folds lower-TF completed Candles -> higher-TF Candles
  StreamEvent            -- emitted when a 1M candle completes for an instrument
  StreamReconnectExhausted -- raised when the stream fails to reconnect
  MultiInstrumentStream  -- reconnecting OANDA stream for N instruments
"""
from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from forex_scalper.models import Candle

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reconnect policy (mirrored from the reference oanda_stream.py)
# ---------------------------------------------------------------------------
_FAILURE_WINDOW_S: float = 60.0
_MAX_FAILURES_IN_WINDOW: int = 3
_RECONNECT_BACKOFF: tuple[float, ...] = (1.0, 2.0)
_READ_TIMEOUT_S: int = 15


# ---------------------------------------------------------------------------
# Bucket maths
# ---------------------------------------------------------------------------

def _bucket_start(at: datetime, minutes: int) -> datetime:
    """Floor *at* to the nearest bucket boundary of *minutes* width.

    Supports minutes in {1, 15, 60} (and any other value <= 1440).
    For minutes >= 60 we floor to the hour; for minutes < 60 we floor within
    the hour -- matching the reference implementation exactly.
    """
    if minutes >= 1440:
        return at.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_since_midnight = at.hour * 60 + at.minute
    bucket = minutes_since_midnight - (minutes_since_midnight % minutes)
    return at.replace(
        hour=bucket // 60,
        minute=bucket % 60,
        second=0,
        microsecond=0,
    )


# ---------------------------------------------------------------------------
# Internal accumulator state
# ---------------------------------------------------------------------------

@dataclass
class _Bucket:
    opened_at: datetime
    open: float
    high: float
    low: float
    close: float


# ---------------------------------------------------------------------------
# TickAggregator -- per-instrument 1M aggregator
# ---------------------------------------------------------------------------

class TickAggregator:
    """Accumulate raw price ticks into completed 1-minute (or arbitrary) candles.

    *instrument* is stored for identification; *minutes* sets the bucket width.
    Call ``add_tick`` for every incoming tick.  A completed candle is returned
    (and never None) exactly when a tick crosses a bucket boundary.
    """

    def __init__(self, instrument: str, minutes: int = 1) -> None:
        self._instrument = instrument
        self._minutes = minutes
        self._current: _Bucket | None = None

    def add_tick(self, price: float, at: datetime) -> Candle | None:
        """Return a completed Candle when a bucket boundary is crossed, else None."""
        bucket_start = _bucket_start(at, self._minutes)

        if self._current is None:
            self._current = _Bucket(
                opened_at=bucket_start,
                open=price,
                high=price,
                low=price,
                close=price,
            )
            return None

        if bucket_start == self._current.opened_at:
            self._current.high = max(self._current.high, price)
            self._current.low = min(self._current.low, price)
            self._current.close = price
            return None

        # Bucket boundary crossed -- emit the completed bucket.
        emitted = Candle(
            time=self._current.opened_at,
            open=self._current.open,
            high=self._current.high,
            low=self._current.low,
            close=self._current.close,
            complete=True,
        )
        self._current = _Bucket(
            opened_at=bucket_start,
            open=price,
            high=price,
            low=price,
            close=price,
        )
        return emitted

    def reset(self) -> None:
        """Discard any in-progress bucket (called on every (re)connection)."""
        self._current = None


# ---------------------------------------------------------------------------
# CandleResampler -- fold lower-TF candles -> higher-TF candles
# ---------------------------------------------------------------------------

class CandleResampler:
    """Resample completed lower-timeframe Candles into a higher timeframe.

    No lookahead: a higher-TF candle is only emitted when the *first* candle
    of the *next* bucket arrives -- proving the current bucket is fully closed.

    Parameters
    ----------
    target_minutes:
        Width of the output bucket in minutes (e.g. 15, 60).
    """

    def __init__(self, target_minutes: int) -> None:
        self._minutes = target_minutes
        self._current: _Bucket | None = None

    def add(self, candle: Candle) -> Candle | None:
        """Fold *candle* into the current bucket; return a completed Candle on
        a bucket boundary, else None."""
        bucket_start = _bucket_start(candle.time, self._minutes)

        if self._current is None:
            self._current = _Bucket(
                opened_at=bucket_start,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
            )
            return None

        if bucket_start == self._current.opened_at:
            self._current.high = max(self._current.high, candle.high)
            self._current.low = min(self._current.low, candle.low)
            self._current.close = candle.close
            return None

        # New bucket -- emit the completed one, then start fresh.
        emitted = Candle(
            time=self._current.opened_at,
            open=self._current.open,
            high=self._current.high,
            low=self._current.low,
            close=self._current.close,
            complete=True,
        )
        self._current = _Bucket(
            opened_at=bucket_start,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
        )
        return emitted

    def reset(self) -> None:
        """Discard any in-progress bucket."""
        self._current = None


# ---------------------------------------------------------------------------
# StreamEvent -- emitted downstream when a 1M candle completes
# ---------------------------------------------------------------------------

@dataclass
class StreamEvent:
    """Emitted by MultiInstrumentStream for every completed 1M candle.

    ``bid`` and ``ask`` are the last tick's prices for that instrument,
    needed downstream for spread calculation, pip-value, and watchdog.
    """

    instrument: str
    candle: Candle
    bid: float
    ask: float


# ---------------------------------------------------------------------------
# StreamReconnectExhausted
# ---------------------------------------------------------------------------

class StreamReconnectExhausted(Exception):
    """Raised when the price stream fails to reconnect within the failure
    window.  Signals an unrecoverable condition; the caller should restart
    the whole bot process."""


# ---------------------------------------------------------------------------
# MultiInstrumentStream
# ---------------------------------------------------------------------------

class MultiInstrumentStream:
    """Reconnecting OANDA pricing stream for multiple instruments.

    Yields :class:`StreamEvent` for every completed 1-minute candle.

    Parameters
    ----------
    token:
        OANDA API token.
    account_id:
        OANDA account ID.
    instruments:
        List of instrument strings, e.g. ``["EUR_USD", "USD_JPY"]``.
    environment:
        ``"practice"`` or ``"live"``.
    now:
        Callable returning monotonic time in seconds (injectable for tests).
    sleep:
        Callable for sleeping (injectable for tests).
    client:
        If provided, used as the v20 API client directly (test seam).
        When ``None``, the real ``oandapyV20.API`` is constructed.
    """

    def __init__(
        self,
        token: str,
        account_id: str,
        instruments: list[str],
        environment: str = "practice",
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        client: Any = None,
    ) -> None:
        self._account_id = account_id
        self._instruments = list(instruments)
        self._now = now
        self._sleep = sleep

        # One TickAggregator per instrument.
        self._aggs: dict[str, TickAggregator] = {
            instr: TickAggregator(instr, 1) for instr in instruments
        }
        # Latest bid/ask per instrument (updated on every tick).
        self._latest_bid: dict[str, float] = {}
        self._latest_ask: dict[str, float] = {}

        if client is not None:
            # Test seam: use provided fake/mock client directly.
            self._client: Any = client
        else:
            # Production: inject OS cert store, then build real API client.
            try:
                import truststore
                truststore.inject_into_ssl()
            except ImportError:
                pass  # optional dependency not installed -- silent
            except Exception as exc:
                log.warning(
                    "truststore.inject_into_ssl() failed; using default ssl cert store: %r",
                    exc,
                )

            import oandapyV20  # type: ignore[import-untyped]
            self._client = oandapyV20.API(
                access_token=token,
                environment=environment,
                request_params={"timeout": _READ_TIMEOUT_S},
            )

    # ------------------------------------------------------------------
    # Public streaming entry point
    # ------------------------------------------------------------------

    def iter_events(self) -> Iterator[StreamEvent]:
        """Yield StreamEvents; reconnect on transient failures.

        Raises :class:`StreamReconnectExhausted` after too many failures
        in the failure window.
        """
        import requests as req_mod  # local import to avoid polluting module scope

        failures: deque[float] = deque()
        while True:
            try:
                yield from self._stream_once()
            except req_mod.exceptions.RequestException as exc:
                now = self._now()
                failures.append(now)
                # Prune failures older than the window.
                while failures and now - failures[0] > _FAILURE_WINDOW_S:
                    failures.popleft()
                if len(failures) >= _MAX_FAILURES_IN_WINDOW:
                    raise StreamReconnectExhausted(
                        f"{len(failures)} stream failures within "
                        f"{_FAILURE_WINDOW_S:.0f}s; giving up"
                    ) from exc
                attempt = len(failures) - 1  # 0-based into backoff schedule
                backoff = _RECONNECT_BACKOFF[min(attempt, len(_RECONNECT_BACKOFF) - 1)]
                log.warning(
                    "stream.reconnect failure_count=%d backoff=%.1f error=%r",
                    len(failures),
                    backoff,
                    exc,
                )
                self._sleep(backoff)

    # ------------------------------------------------------------------
    # Internal: one stream connection (terminates when message source
    # is exhausted or raises; never loops internally)
    # ------------------------------------------------------------------

    def _stream_once(self) -> Iterator[StreamEvent]:
        """Connect once and yield events until the stream ends or raises.

        On entry, all aggregators are reset so stale partial candles from
        before a disconnect are never emitted.
        """
        import oandapyV20.endpoints.pricing as v20_pricing  # type: ignore[import-untyped]

        for agg in self._aggs.values():
            agg.reset()

        req = v20_pricing.PricingStream(
            accountID=self._account_id,
            params={"instruments": ",".join(self._instruments)},
        )
        msg: dict[str, Any]
        for msg in self._client.request(req):
            if msg.get("type") != "PRICE":
                continue

            instrument: str = msg["instrument"]
            bid = float(msg["bids"][0]["price"])
            ask = float(msg["asks"][0]["price"])
            mid = (bid + ask) / 2.0
            at = datetime.fromisoformat(msg["time"].replace("Z", "+00:00"))

            self._latest_bid[instrument] = bid
            self._latest_ask[instrument] = ask

            if instrument not in self._aggs:
                continue  # unexpected instrument -- skip
            agg = self._aggs[instrument]

            emitted = agg.add_tick(mid, at)
            if emitted is not None:
                yield StreamEvent(
                    instrument=instrument,
                    candle=emitted,
                    bid=bid,
                    ask=ask,
                )
