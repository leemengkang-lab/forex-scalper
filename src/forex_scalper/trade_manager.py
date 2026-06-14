"""
trade_manager.py
================
Runs every candle over OPEN trades. Three jobs:

  * Time stop  — a stalled scalp is a failed scalp. If a trade hasn't done its
    job within `max_minutes` and isn't meaningfully in profit, close it.
  * Trailing   — only after the trade is clearly in profit (optional, off-ish).
  * Break-even — OFF by default. Your own backtest showed it hurt; leave it off
    unless a clean walk-forward test says otherwise.

It asks the broker to close; it does not size or open anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from execution import Broker, OpenTrade
from models import pip_size

logger = logging.getLogger("trade_manager")


@dataclass
class TradeManagerConfig:
    max_minutes: int = 12           # time stop
    min_progress_pips: float = 2.0  # "doing its job" threshold to survive the time stop
    use_trailing: bool = False
    trail_start_pips: float = 8.0   # only trail once this far in profit
    trail_distance_pips: float = 5.0
    use_break_even: bool = False    # keep OFF


class TradeManager:
    def __init__(self, cfg: TradeManagerConfig, broker: Broker):
        self.cfg = cfg
        self.broker = broker

    def manage(self, now: datetime = None) -> None:
        now = now or datetime.now(timezone.utc)
        for t in self.broker.open_trades():
            self._manage_one(t, now)

    def _manage_one(self, t: OpenTrade, now: datetime) -> None:
        bid, ask = self.broker.get_price(t.instrument)
        price = bid if t.units > 0 else ask
        pip = pip_size(t.instrument)
        gain_pips = (price - t.entry_price) / pip * (1 if t.units > 0 else -1)
        age_min = (now - t.opened_at).total_seconds() / 60

        # time stop
        if age_min >= self.cfg.max_minutes and gain_pips < self.cfg.min_progress_pips:
            logger.info("Time stop %s (%.1f min, %.1f pips)", t.trade_id, age_min, gain_pips)
            self.broker.close_trade(t.trade_id)
            return

        # trailing (optional)
        if self.cfg.use_trailing and gain_pips >= self.cfg.trail_start_pips:
            new_stop = price - t.units / abs(t.units) * self.cfg.trail_distance_pips * pip
            improves = (new_stop > t.stop_price) if t.units > 0 else (new_stop < t.stop_price)
            if improves:
                t.stop_price = new_stop
                # TODO(live): push the modified stop to the broker (trades.TradeCRCDO)
                logger.debug("Trail %s -> stop %.5f", t.trade_id, new_stop)
