"""
execution.py
============
Broker abstraction so the bot logic never touches a vendor SDK directly.

  * Broker        — the interface every broker must implement.
  * PaperBroker   — in-memory fills for demo / shadow mode. Runs with no creds.
  * OandaBroker   — stub showing exactly where to call the v20 API. The import
                    is lazy so the rest of the bot runs without oandapyV20.

The bot calls: get_price, place_market_order, close_trade, open_trades.
"""

from __future__ import annotations

import itertools
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from forex_scalper.models import pip_size

logger = logging.getLogger("execution")


@dataclass
class OpenTrade:
    trade_id: str
    instrument: str
    units: int                 # signed: + long, - short
    entry_price: float
    stop_price: float
    take_profit: float
    opened_at: datetime
    setup: str = ""


class Broker(ABC):
    @abstractmethod
    def get_price(self, instrument: str) -> Tuple[float, float]:
        """(bid, ask)."""

    @abstractmethod
    def place_market_order(self, instrument: str, units: int, stop: float,
                           take_profit: float, setup: str = "") -> Optional[OpenTrade]:
        ...

    @abstractmethod
    def close_trade(self, trade_id: str) -> Optional[float]:
        """Returns realised P&L in account ccy, or None if not found."""

    @abstractmethod
    def open_trades(self) -> List[OpenTrade]:
        ...


class PaperBroker(Broker):
    """
    Deterministic in-memory broker. Prices are pushed in via set_price so a
    backtest / shadow run is fully reproducible. P&L is computed from the
    pip_value you provide per instrument.
    """
    def __init__(self, pip_value: Dict[str, float]):
        self._prices: Dict[str, Tuple[float, float]] = {}
        self._pip_value = pip_value
        self._trades: Dict[str, OpenTrade] = {}
        self._ids = itertools.count(1)

    def set_price(self, instrument: str, bid: float, ask: float) -> None:
        self._prices[instrument] = (bid, ask)

    def get_price(self, instrument: str) -> Tuple[float, float]:
        return self._prices[instrument]

    def place_market_order(self, instrument, units, stop, take_profit, setup="") -> Optional[OpenTrade]:
        bid, ask = self._prices[instrument]
        fill = ask if units > 0 else bid       # pay the spread, like real life
        tid = f"P{next(self._ids)}"
        t = OpenTrade(tid, instrument, units, fill, stop, take_profit,
                      datetime.now(timezone.utc), setup)
        self._trades[tid] = t
        logger.info("PAPER fill %s %s %+d @ %.5f (sl %.5f tp %.5f)",
                    tid, instrument, units, fill, stop, take_profit)
        return t

    def close_trade(self, trade_id: str) -> Optional[float]:
        t = self._trades.pop(trade_id, None)
        if not t:
            return None
        bid, ask = self._prices[t.instrument]
        exit_price = bid if t.units > 0 else ask
        pip = pip_size(t.instrument)
        pips = (exit_price - t.entry_price) / pip * (1 if t.units > 0 else -1)
        pnl = pips * abs(t.units) * self._pip_value.get(t.instrument, 0.0)
        logger.info("PAPER close %s pnl=%.2f (%.1f pips)", trade_id, pnl, pips)
        return pnl

    def open_trades(self) -> List[OpenTrade]:
        return list(self._trades.values())


class OandaBroker(Broker):
    """
    Live OANDA v20 broker. Fill in the TODOs with oandapyV20 calls. Lazy import
    keeps the package runnable without the SDK installed.
    """
    def __init__(self, account_id: str, token: str, practice: bool = True):
        from oandapyV20 import API  # lazy
        env = "practice" if practice else "live"
        self.account_id = account_id
        self.api = API(access_token=token, environment=env)

    def get_price(self, instrument: str) -> Tuple[float, float]:
        # TODO: pricing.PricingInfo -> return (bid, ask)
        raise NotImplementedError

    def place_market_order(self, instrument, units, stop, take_profit, setup="") -> Optional[OpenTrade]:
        # TODO: orders.OrderCreate with a MARKET order + stopLossOnFill +
        # takeProfitOnFill. Parse the fill into an OpenTrade. Make it idempotent
        # (use a client order id) so a retry never double-fires.
        raise NotImplementedError

    def close_trade(self, trade_id: str) -> Optional[float]:
        # TODO: trades.TradeClose -> parse realizedPL
        raise NotImplementedError

    def open_trades(self) -> List[OpenTrade]:
        # TODO: trades.OpenTrades -> map to OpenTrade list
        raise NotImplementedError
