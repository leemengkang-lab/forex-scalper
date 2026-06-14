"""
live_engine.py
==============
Synchronous core of the live trading loop.

  CrossRateBook  — keeps account-ccy cross rates fresh in MarketState so that
                   position sizing always has a valid pip value.
  LiveEngine     — turns each completed-candle StreamEvent into MarketState
                   updates + (for tradeable instruments) one run of the trading
                   pipeline.

No asyncio, no threads, no real network — fully deterministic and unit-testable.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from forex_scalper.bot import ScalpBot
from forex_scalper.config import BotConfig
from forex_scalper.data import MarketState
from forex_scalper.execution import Broker
from forex_scalper.pip_value import quote_ccy
from forex_scalper.stream import CandleResampler, StreamEvent

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CrossRateBook
# ---------------------------------------------------------------------------

class CrossRateBook:
    """Keeps MarketState cross rates current for position sizing.

    For each tradeable instrument whose quote currency differs from the account
    currency, we need "account-ccy per 1 unit of quote ccy".  This class
    queries the broker (throttled) and writes those rates into MarketState.

    Resolution strategy
    -------------------
    For a needed ccy (e.g. USD when account is SGD):

      1. Try direct  ``USD_SGD``  -> rate = mid
      2. Try inverted ``SGD_USD``  -> rate = 1 / mid

    Whichever succeeds first is cached.  If both fail, the ccy is skipped and
    its rate is left unset; MarketState then returns 0.0 for pip_value_per_unit
    which causes the risk manager to reject, i.e. fail-safe.
    """

    def __init__(
        self,
        broker: Broker,
        account_ccy: str,
        instruments: list[str],
        *,
        refresh_secs: float = 30.0,
    ) -> None:
        self._broker = broker
        self._account = account_ccy.upper()
        self._refresh_secs = refresh_secs

        # Sorted for deterministic iteration (helps tests).
        self._needed: list[str] = sorted(
            {quote_ccy(i) for i in instruments if quote_ccy(i) != self._account}
        )

        # ccy -> (oanda_instrument, invert)
        self._instr_for: dict[str, tuple[str, bool]] = {}

        self._last_refresh: float | None = None

    # ------------------------------------------------------------------

    def refresh(self, market: MarketState, now_monotonic: float) -> None:
        """Refresh all needed cross rates into *market* (throttled)."""
        if (
            self._last_refresh is not None
            and now_monotonic - self._last_refresh < self._refresh_secs
        ):
            return

        self._last_refresh = now_monotonic

        for ccy in self._needed:
            try:
                self._refresh_one(market, ccy)
            except Exception:
                log.warning(
                    "CrossRateBook: failed to refresh rate for %s/%s; skipping",
                    ccy,
                    self._account,
                    exc_info=True,
                )

    def _refresh_one(self, market: MarketState, ccy: str) -> None:
        """Resolve (once) and update the cross rate for a single currency."""
        if ccy not in self._instr_for:
            # Try to resolve the instrument.
            direct = f"{ccy}_{self._account}"
            inverted = f"{self._account}_{ccy}"
            try:
                self._broker.get_price(direct)
                self._instr_for[ccy] = (direct, False)
            except Exception:
                try:
                    self._broker.get_price(inverted)
                    self._instr_for[ccy] = (inverted, True)
                except Exception:
                    log.warning(
                        "CrossRateBook: cannot find instrument for %s/%s "
                        "(tried %s and %s); rate will remain unset",
                        ccy,
                        self._account,
                        direct,
                        inverted,
                    )
                    return  # leave rate unset -> risk manager will reject

        instrument, invert = self._instr_for[ccy]
        bid, ask = self._broker.get_price(instrument)
        mid = (bid + ask) / 2.0
        rate = (1.0 / mid) if invert else mid
        market.set_cross_rate(ccy, rate)
        log.debug(
            "CrossRateBook: set %s/%s = %.6f (instrument=%s invert=%s)",
            ccy,
            self._account,
            rate,
            instrument,
            invert,
        )


# ---------------------------------------------------------------------------
# LiveEngine
# ---------------------------------------------------------------------------

class LiveEngine:
    """Synchronous event processor: StreamEvent -> MarketState -> bot pipeline.

    For every StreamEvent the engine:

      1. Updates the live price in MarketState.
      2. Adds the 1M candle to MarketState.
      3. For *tradeable* instruments only:
         a. Resamples to 15M and 1H and adds any completed HTF candle.
         b. Throttle-refreshes cross rates via CrossRateBook.
         c. Calls ``bot.on_candle_close``.

    Non-tradeable instruments (e.g. rate sources) still get price + 1M candle
    so they can serve as data for indicators, but the bot pipeline is skipped.
    """

    def __init__(
        self,
        cfg: BotConfig,
        market: MarketState,
        broker: Broker,
        bot: ScalpBot,
        *,
        account_ccy: str,
        tradeable: list[str],
        rate_refresh_secs: float = 30.0,
        now_monotonic: Callable[[], float] = time.monotonic,
        now_utc: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        market.set_account_ccy(account_ccy)

        self.market = market
        self._bot = bot
        self._tradeable: set[str] = set(tradeable)

        # Per-instrument resamplers (only for tradeable instruments).
        self._r15: dict[str, CandleResampler] = {
            i: CandleResampler(15) for i in tradeable
        }
        self._r60: dict[str, CandleResampler] = {
            i: CandleResampler(60) for i in tradeable
        }

        self._rates = CrossRateBook(
            broker, account_ccy, tradeable, refresh_secs=rate_refresh_secs
        )

        self._now_monotonic = now_monotonic
        self._now_utc = now_utc

    # ------------------------------------------------------------------

    def process_event(
        self, ev: StreamEvent, *, now: datetime | None = None
    ) -> None:
        """Process one completed-candle StreamEvent.

        Errors are logged and swallowed so that a single bad event never kills
        the caller's loop.  (ScalpBot.on_candle_close already has its own
        guard internally; LiveEngine adds a coarser outer guard for any
        unexpected exceptions in the bookkeeping code.)
        """
        try:
            self._process(ev, now=now)
        except Exception:
            log.exception(
                "LiveEngine.process_event: unexpected error for %s; continuing",
                ev.instrument,
            )

    def _process(self, ev: StreamEvent, *, now: datetime | None) -> None:
        # Always update price + 1M candle regardless of tradeability.
        self.market.set_price(ev.instrument, ev.bid, ev.ask)
        self.market.add_candle(ev.instrument, "1M", ev.candle)

        if ev.instrument not in self._tradeable:
            return  # only price + candle needed; skip bot pipeline

        # Resample to higher timeframes.
        c15 = self._r15[ev.instrument].add(ev.candle)
        if c15 is not None:
            self.market.add_candle(ev.instrument, "15M", c15)

        c60 = self._r60[ev.instrument].add(ev.candle)
        if c60 is not None:
            self.market.add_candle(ev.instrument, "1H", c60)

        # Refresh cross rates (throttled by CrossRateBook).
        self._rates.refresh(self.market, self._now_monotonic())

        # Run the trading pipeline.
        self._bot.on_candle_close(ev.instrument, now or self._now_utc())
