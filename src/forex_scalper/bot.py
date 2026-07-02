"""
bot.py
======
The orchestrator. Wires every module together and runs the pipeline on each
closed 1-minute candle:

    manage open trades  ->  session/news gate  ->  bias  ->  regime  ->
    setup detect  ->  risk manager (veto + sizing)  ->  execute  ->  journal

The risk manager holds the veto; the broker is swappable (paper for demo/shadow,
OANDA for live). Nothing trades real money until config.live is explicitly True.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from forex_scalper import bias as bias_mod
from forex_scalper import regime as regime_mod
from forex_scalper import setups as setups_mod
from forex_scalper.config import BotConfig
from forex_scalper.data import MarketState
from forex_scalper.execution import Broker
from forex_scalper.journal import Journal, JournalRow
from forex_scalper.models import Bias, SetupSignal, pip_size
from forex_scalper.notifier import ConsoleNotifier, Notifier
from forex_scalper.persistence import Repository
from forex_scalper.risk_manager import RiskManager, TradeSignal
from forex_scalper.session import SessionFilter
from forex_scalper.trade_manager import TradeManager

logger = logging.getLogger("bot")


class ScalpBot:
    def __init__(self, cfg: BotConfig, market: MarketState, broker: Broker,
                 notifier: Notifier | None = None, repo: Repository | None = None):
        self.cfg = cfg
        self.market = market
        self.broker = broker
        self.notifier = notifier or ConsoleNotifier()
        self.repo = repo
        self.journal = Journal(cfg.journal_path)
        self.session = SessionFilter(cfg.session)
        self.trades = TradeManager(cfg.trade, broker)
        self.risk = RiskManager(cfg.risk, cfg.starting_balance, cfg.usd_sign)

    # ---- called by the run loop whenever a 1M candle closes ------------- #
    def on_candle_close(self, instrument: str, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        try:
            self.trades.manage(now)                      # 1) always manage open trades

            if not self.session.can_enter(instrument, now):
                return                                   # 2) outside window / news

            bias = bias_mod.get_bias(self.market, instrument, self.cfg.bias)   # 3)
            regime = regime_mod.get_regime(self.market, instrument, self.cfg.regime)  # 4)
            sig = setups_mod.detect(self.market, instrument, bias, regime, self.cfg.setup)  # 5)
            if sig is None:
                return

            self._try_enter(sig, bias, regime, now)      # 6-7) risk + execute
        except Exception as e:                           # connectivity guard
            logger.exception("on_candle_close failed for %s: %s", instrument, e)
            self.notifier.send(f"⚠️ error on {instrument}: {e}")

    # -------------------------------------------------------------------- #
    def _try_enter(self, sig: SetupSignal, bias, regime, now) -> None:
        pv = self.market.pip_value_per_unit(sig.instrument)
        atr = self.market.atr(sig.instrument, self.cfg.setup.timeframe)
        atr_pips = (atr / pip_size(sig.instrument)) if atr else 0.0

        tsig = TradeSignal(
            instrument=sig.instrument, direction=sig.direction,
            entry_price=sig.entry_price, stop_price=sig.stop_price,
            pip_value_per_unit=pv, atr_pips=atr_pips,
            spread_pips=sig.spread_pips, setup=sig.setup,
        )
        decision = self.risk.evaluate(tsig, now)

        base = JournalRow(
            instrument=sig.instrument, setup=sig.setup, direction=sig.direction,
            bias=bias.value, regime=regime.value, entry=sig.entry_price,
            stop=sig.stop_price, spread_pips=sig.spread_pips, atr_pips=round(atr_pips, 1),
            note=sig.note,
        )

        # strict bias alignment (belt-and-suspenders; inversion removed, Setup B off)
        if (bias == Bias.LONG_ONLY and sig.direction < 0) or \
           (bias == Bias.SHORT_ONLY and sig.direction > 0):
            base.event = "reject"; base.exit_reason = "bias_mismatch"
            self.journal.log(base)
            return

        if not decision.approved:
            base.event = "reject"; base.exit_reason = decision.reason.value
            self.journal.log(base)
            return

        tp = decision.take_profit          # risk_manager owns TP (1.3x the stop)

        trade = self.broker.place_market_order(
            sig.instrument, decision.units, decision.stop_price, round(tp, 5), sig.setup)
        if trade is None:
            base.event = "reject"; base.exit_reason = "broker_no_fill"
            self.journal.log(base); return

        self.risk.register_fill(sig.instrument, sig.direction, decision.risk_amount)

        if self.repo is not None:
            self.repo.record_open(trade, direction=sig.direction, risk_amount=decision.risk_amount)

        base.event = "open"
        base.stop = decision.stop_price; base.take_profit = round(tp, 5)
        base.stop_pips = decision.stop_pips; base.units = decision.units
        base.risk_amount = decision.risk_amount
        self.journal.log(base)
        self.notifier.send(
            f"✅ {sig.setup} {sig.instrument} {'LONG' if sig.direction>0 else 'SHORT'} "
            f"{decision.units:+d} @ {trade.entry_price:.5f} sl {decision.stop_price:.5f} "
            f"tp {tp:.5f} ({decision.note or 'ok'})")

    # ---- the run loop (skeleton — wire to your stream/poll) ------------- #
    def run(self):
        """
        Pseudocode for the live loop. In practice you subscribe to OANDA's
        pricing/candle stream and call on_candle_close() when a 1M candle
        completes, after pushing the new candle + price into MarketState.
        """
        raise NotImplementedError(
            "Wire this to your OANDA stream: on each completed 1M candle, "
            "update MarketState (candles + price + pip_value), then call "
            "on_candle_close(instrument). Keep config.live=False until proven.")
