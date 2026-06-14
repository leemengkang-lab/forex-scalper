"""
warmup.py
=========
Preload MarketState with recent historical candles so bias/regime indicators
have data immediately at startup — avoiding hours of FLAT state after boot.

Usage::

    from forex_scalper.warmup import warm_up
    warm_up(market, broker, ["EUR_USD", "GBP_USD"])
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("warmup")

_DEFAULT_COUNTS: dict[str, int] = {"1H": 120, "15M": 200, "1M": 200}
# Load 1H and 15M before 1M so higher-timeframe context arrives first.
_TIMEFRAME_ORDER = ("1H", "15M", "1M")


def warm_up(
    market: Any,
    broker: Any,
    instruments: list[str],
    *,
    counts: dict[str, int] | None = None,
    log: logging.Logger | None = None,
) -> None:
    """Preload MarketState with recent history for 1M/15M/1H so bias/regime
    have data immediately at startup. Fetches via broker.fetch_candles and
    adds each candle to MarketState. Resilient: a failure for one
    (instrument, timeframe) logs and is skipped, never aborts the rest."""
    _log = log or logger
    effective_counts = {**_DEFAULT_COUNTS, **(counts or {})}

    for instrument in instruments:
        for tf in _TIMEFRAME_ORDER:
            count = effective_counts.get(tf, 200)
            try:
                candles = broker.fetch_candles(instrument, tf, count)
                for candle in candles:
                    market.add_candle(instrument, tf, candle)
                _log.info(
                    "warmup: loaded %d candles %s %s", len(candles), instrument, tf
                )
            except Exception as exc:
                _log.warning(
                    "warmup: skipping %s %s — %r", instrument, tf, exc
                )
