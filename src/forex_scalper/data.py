"""
data.py
=======
MarketState — the single source of truth. Every other module reads from here;
nobody fetches its own data. The orchestrator feeds it completed candles from
the broker stream, and MarketState maintains rolling windows + cheap indicator
accessors on top.

Timeframes used:
    "1M"  -> execution (setup detection)
    "15M" -> regime
    "1H"  -> bias
"""

from __future__ import annotations

from collections import defaultdict, deque

from forex_scalper import indicators as ind
from forex_scalper.models import Candle, pip_size
from forex_scalper.pip_value import pip_value_per_unit as _pv
from forex_scalper.pip_value import quote_ccy

MAXLEN = 500  # how many candles to keep per (instrument, timeframe)


class MarketState:
    def __init__(self) -> None:
        # candles[instrument][timeframe] -> deque[Candle]
        self._c: dict[str, dict[str, deque[Candle]]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=MAXLEN)))
        self._bid: dict[str, float] = {}
        self._ask: dict[str, float] = {}
        # pip value (account ccy per pip per unit). Explicit overrides for
        # backtests/demo; computed from cross rates for live trading.
        self._pip_value: dict[str, float] = {}
        # live account currency and cross rates (ccy -> units of account ccy)
        self._account_ccy: str | None = None
        self._cross_to_account: dict[str, float] = {}

    # --- ingest ----------------------------------------------------------- #
    def add_candle(self, instrument: str, tf: str, candle: Candle) -> None:
        if candle.complete:
            self._c[instrument][tf].append(candle)

    def set_price(self, instrument: str, bid: float, ask: float) -> None:
        self._bid[instrument] = bid
        self._ask[instrument] = ask

    def set_pip_value(self, instrument: str, value: float) -> None:
        self._pip_value[instrument] = value

    def set_account_ccy(self, ccy: str) -> None:
        self._account_ccy = ccy.upper()

    def set_cross_rate(self, ccy: str, rate_to_account: float) -> None:
        self._cross_to_account[ccy.upper()] = rate_to_account

    # --- accessors -------------------------------------------------------- #
    def candles(self, instrument: str, tf: str, n: int | None = None) -> list[Candle]:
        seq = list(self._c[instrument][tf])
        return seq[-n:] if n else seq

    def closes(self, instrument: str, tf: str, n: int | None = None) -> list[float]:
        return [c.close for c in self.candles(instrument, tf, n)]

    def mid(self, instrument: str) -> float | None:
        b, a = self._bid.get(instrument), self._ask.get(instrument)
        return None if b is None or a is None else (b + a) / 2

    def spread_pips(self, instrument: str) -> float | None:
        b, a = self._bid.get(instrument), self._ask.get(instrument)
        if b is None or a is None:
            return None
        return (a - b) / pip_size(instrument)

    def pip_value_per_unit(self, instrument: str) -> float:
        # Explicit override takes precedence (preserves backtest/demo path).
        if instrument in self._pip_value:
            return self._pip_value[instrument]
        # Account currency must be wired before live use; 0.0 causes risk
        # manager to reject, which is the safe fallback.
        if self._account_ccy is None:
            return 0.0
        q = quote_ccy(instrument)
        if q == self._account_ccy:
            return _pv(instrument, self._account_ccy, lambda c: 1.0)
        rate = self._cross_to_account.get(q)
        if rate is None:
            return 0.0  # cross rate not yet known -> risk manager will reject
        return _pv(instrument, self._account_ccy, lambda c: rate)

    def ema(self, instrument: str, tf: str, period: int) -> float | None:
        return ind.ema(self.closes(instrument, tf), period)

    def atr(self, instrument: str, tf: str, period: int = 14) -> float | None:
        return ind.atr(self.candles(instrument, tf), period)

    def atr_pips(self, instrument: str, tf: str, period: int = 14) -> float | None:
        a = self.atr(instrument, tf, period)
        return None if a is None else a / pip_size(instrument)
