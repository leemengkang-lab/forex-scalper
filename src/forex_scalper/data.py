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
from typing import Deque, Dict, List, Optional

from forex_scalper import indicators as ind
from forex_scalper.models import Candle, pip_size

MAXLEN = 500  # how many candles to keep per (instrument, timeframe)


class MarketState:
    def __init__(self):
        # candles[instrument][timeframe] -> deque[Candle]
        self._c: Dict[str, Dict[str, Deque[Candle]]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=MAXLEN)))
        self._bid: Dict[str, float] = {}
        self._ask: Dict[str, float] = {}
        # pip value (account ccy per pip per unit). Set by the data/execution
        # layer from live rates. Default 0 forces you to wire it before going live.
        self._pip_value: Dict[str, float] = {}

    # --- ingest ----------------------------------------------------------- #
    def add_candle(self, instrument: str, tf: str, candle: Candle) -> None:
        if candle.complete:
            self._c[instrument][tf].append(candle)

    def set_price(self, instrument: str, bid: float, ask: float) -> None:
        self._bid[instrument] = bid
        self._ask[instrument] = ask

    def set_pip_value(self, instrument: str, value: float) -> None:
        self._pip_value[instrument] = value

    # --- accessors -------------------------------------------------------- #
    def candles(self, instrument: str, tf: str, n: Optional[int] = None) -> List[Candle]:
        seq = list(self._c[instrument][tf])
        return seq[-n:] if n else seq

    def closes(self, instrument: str, tf: str, n: Optional[int] = None) -> List[float]:
        return [c.close for c in self.candles(instrument, tf, n)]

    def mid(self, instrument: str) -> Optional[float]:
        b, a = self._bid.get(instrument), self._ask.get(instrument)
        return None if b is None or a is None else (b + a) / 2

    def spread_pips(self, instrument: str) -> Optional[float]:
        b, a = self._bid.get(instrument), self._ask.get(instrument)
        if b is None or a is None:
            return None
        return (a - b) / pip_size(instrument)

    def pip_value_per_unit(self, instrument: str) -> float:
        # TODO(live): compute from current quote + account ccy cross rate.
        return self._pip_value.get(instrument, 0.0)

    def ema(self, instrument: str, tf: str, period: int) -> Optional[float]:
        return ind.ema(self.closes(instrument, tf), period)

    def atr(self, instrument: str, tf: str, period: int = 14) -> Optional[float]:
        return ind.atr(self.candles(instrument, tf), period)

    def atr_pips(self, instrument: str, tf: str, period: int = 14) -> Optional[float]:
        a = self.atr(instrument, tf, period)
        return None if a is None else a / pip_size(instrument)
