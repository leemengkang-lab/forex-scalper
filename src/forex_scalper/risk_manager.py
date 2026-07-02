"""
risk_manager.py
===============
Deterministic risk manager for a forex scalping bot.

Design principles
-----------------
* No AI, no discretion, no surprises. Same inputs -> same decision, every time.
* It is the ONLY thing that approves position size, and it can VETO any trade.
* It owns the kill switches: daily loss limit + loss-streak de-risking.
* It treats correlated pairs as shared exposure, so five USD bets don't
  masquerade as five independent trades.

What it does NOT do (on purpose)
--------------------------------
* It does not fetch data or talk to the broker. Your data/execution layers do
  that and feed it numbers. This keeps it pure and testable.
* It does not know live FX cross rates. The caller passes `pip_value_per_unit`
  (value, in your ACCOUNT currency, of a 1-pip move per 1 unit). For OANDA that
  is computed from the current quote + your account ccy (SGD in your case).

Wire-up checklist
-----------------
1. On every signal: call `evaluate(signal)` -> get a RiskDecision.
2. On a confirmed fill: call `register_fill(...)` so heat/correlation track it.
3. On a closed trade: call `close_position(instrument, pnl)` so the daily P&L
   and loss-streak logic update (this is what arms the kill switch).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

logger = logging.getLogger("risk_manager")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01        # 1% of balance risked per trade
    max_gross_heat: float = 0.03        # 3% total open risk across ALL trades
    max_factor_exposure: float = 0.02   # 2% net directional bet on one factor (USD)
    daily_loss_limit: float = 0.03      # halt for the day after -3% realised
    max_concurrent_positions: int = 4
    min_stop_pips_default: float = 10.0 # floor for non-JPY (your backtest finding)
    min_stop_pips_jpy: float = 15.0     # floor for JPY pairs
    max_spread_pips: float = 1.5        # skip entries when spread is wider
    stop_atr_mult: float = 1.5          # stop distance = this * 1M ATR (pips)
    tp_r_multiple: float = 1.3          # take-profit = this * stop (>=1 -> paid >= risked)
    min_atr_pips: float = 2.5           # skip entries when 1M ATR is below this (too dead)
    derisk_after_losses: int = 3        # consecutive losses -> shrink size
    derisk_factor: float = 0.5          # ...to half, until a winner resets it
    day_rollover_hour_utc: int = 0      # when the trading "day" resets (UTC)


# instrument -> sign of USD exposure when LONG the pair.
#   long EUR_USD  => short USD => -1
#   long USD_JPY  => long  USD => +1
# Pairs not listed are treated as uncorrelated to USD (factor contribution 0).
DEFAULT_USD_SIGN = {
    "EUR_USD": -1, "GBP_USD": -1, "AUD_USD": -1, "NZD_USD": -1,
    "USD_JPY": +1, "USD_CHF": +1, "USD_CAD": +1,
}


def pip_size(instrument: str) -> float:
    """0.01 for JPY quote pairs, else 0.0001."""
    return 0.01 if instrument.upper().endswith("JPY") else 0.0001


# --------------------------------------------------------------------------- #
# Data objects
# --------------------------------------------------------------------------- #
@dataclass
class TradeSignal:
    instrument: str
    direction: int          # +1 long, -1 short
    entry_price: float
    stop_price: float       # where the setup wants the stop (structural)
    pip_value_per_unit: float  # ACCOUNT-ccy value of 1 pip per 1 unit (caller computes)
    atr_pips: float = 0.0      # 1M ATR in pips (caller computes); drives stop + min-vol veto
    spread_pips: float = 0.0
    setup: str = ""


@dataclass
class OpenPosition:
    instrument: str
    direction: int
    risk_amount: float      # account-ccy at risk if stopped


class Reject(Enum):
    OK = "approved"
    HALTED = "trading halted (kill switch active)"
    DAILY_LIMIT = "daily loss limit reached"
    MAX_POSITIONS = "max concurrent positions reached"
    DUPLICATE = "already have a position in this instrument"
    GROSS_HEAT = "would exceed total open-risk cap"
    FACTOR_EXPOSURE = "would exceed correlated (USD) exposure cap"
    SPREAD = "spread too wide to enter"
    LOW_VOLATILITY = "1M ATR below minimum; market too quiet to scalp"
    BAD_SIZING = "computed size was not positive"


@dataclass
class RiskDecision:
    approved: bool
    reason: Reject
    units: int = 0
    stop_price: float = 0.0   # may be widened to respect the min floor
    stop_pips: float = 0.0
    take_profit: float = 0.0
    risk_amount: float = 0.0
    note: str = ""


# --------------------------------------------------------------------------- #
# Risk manager
# --------------------------------------------------------------------------- #
class RiskManager:
    def __init__(
        self,
        config: RiskConfig,
        balance: float,
        usd_sign: dict[str, int] | None = None,
        now: datetime | None = None,
    ):
        self.cfg = config
        self.balance = float(balance)
        self.usd_sign = dict(usd_sign) if usd_sign is not None else dict(DEFAULT_USD_SIGN)
        self.open_positions: list[OpenPosition] = []
        self.halted = False
        self._consecutive_losses = 0

        now = now or datetime.now(UTC)
        self._current_day = self._trading_day(now)
        self._day_start_balance = self.balance
        self._day_realized_pnl = 0.0

    # ----- introspection helpers (handy for logging / Telegram) ----------- #
    @property
    def gross_heat(self) -> float:
        return sum(p.risk_amount for p in self.open_positions)

    @property
    def net_usd_exposure(self) -> float:
        """Signed account-ccy risk net long(+)/short(-) USD across open trades."""
        return sum(
            p.direction * self.usd_sign.get(p.instrument, 0) * p.risk_amount
            for p in self.open_positions
        )

    @property
    def daily_pnl(self) -> float:
        return self._day_realized_pnl

    @property
    def daily_room(self) -> float:
        """Account-ccy of loss still allowed today before the kill switch."""
        limit = self.cfg.daily_loss_limit * self._day_start_balance
        return max(0.0, limit + self._day_realized_pnl)  # pnl is negative when losing

    # ----- day rollover --------------------------------------------------- #
    def _trading_day(self, now: datetime) -> datetime:
        shifted = now.astimezone(UTC) - timedelta(hours=self.cfg.day_rollover_hour_utc)
        return shifted.replace(hour=0, minute=0, second=0, microsecond=0)

    def _maybe_rollover(self, now: datetime) -> None:
        day = self._trading_day(now)
        if day != self._current_day:
            logger.info("Day rollover -> resetting daily P&L and kill switch")
            self._current_day = day
            self._day_start_balance = self.balance
            self._day_realized_pnl = 0.0
            self.halted = False  # a new day clears the daily-limit halt

    # ----- outcomes (caller reports these) -------------------------------- #
    def register_fill(self, instrument: str, direction: int, risk_amount: float) -> None:
        self.open_positions.append(OpenPosition(instrument, direction, risk_amount))

    def close_position(self, instrument: str, pnl: float, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self._maybe_rollover(now)

        # remove one matching open position
        for i, p in enumerate(self.open_positions):
            if p.instrument == instrument:
                self.open_positions.pop(i)
                break

        self.balance += pnl
        self._day_realized_pnl += pnl

        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        # arm the daily kill switch
        if self._day_realized_pnl <= -self.cfg.daily_loss_limit * self._day_start_balance:
            if not self.halted:
                logger.warning(
                    "DAILY LOSS LIMIT hit (%.2f). Halting new trades until rollover.",
                    self._day_realized_pnl,
                )
            self.halted = True

    # ----- the core decision ---------------------------------------------- #
    def evaluate(self, signal: TradeSignal, now: datetime | None = None) -> RiskDecision:
        now = now or datetime.now(UTC)
        self._maybe_rollover(now)

        def deny(reason: Reject, note: str = "") -> RiskDecision:
            return RiskDecision(False, reason, note=note)

        # 1) kill switches first
        if self.halted:
            return deny(Reject.HALTED)
        if self._day_realized_pnl <= -self.cfg.daily_loss_limit * self._day_start_balance:
            self.halted = True
            return deny(Reject.DAILY_LIMIT)

        # 2) cheap gates
        if signal.spread_pips > self.cfg.max_spread_pips:
            return deny(Reject.SPREAD, f"spread {signal.spread_pips:.2f} > {self.cfg.max_spread_pips}")
        if signal.atr_pips < self.cfg.min_atr_pips:
            return deny(Reject.LOW_VOLATILITY,
                        f"atr {signal.atr_pips:.2f}p < {self.cfg.min_atr_pips}p")
        if any(p.instrument == signal.instrument for p in self.open_positions):
            return deny(Reject.DUPLICATE)
        if len(self.open_positions) >= self.cfg.max_concurrent_positions:
            return deny(Reject.MAX_POSITIONS)

        # 3) ATR-based stop (floored) and a take-profit that is always >= the stop
        psize = pip_size(signal.instrument)
        floor = self.cfg.min_stop_pips_jpy if signal.instrument.upper().endswith("JPY") \
            else self.cfg.min_stop_pips_default
        stop_pips = max(self.cfg.stop_atr_mult * signal.atr_pips, floor)
        tp_pips = self.cfg.tp_r_multiple * stop_pips
        stop_price = signal.entry_price - signal.direction * stop_pips * psize
        take_profit = signal.entry_price + signal.direction * tp_pips * psize

        # 4) risk amount (with loss-streak de-risking)
        risk_amount = self.cfg.risk_per_trade * self.balance
        if self._consecutive_losses >= self.cfg.derisk_after_losses:
            risk_amount *= self.cfg.derisk_factor

        # 5) portfolio caps -- check BEFORE sizing units
        if self.gross_heat + risk_amount > self.cfg.max_gross_heat * self.balance:
            return deny(Reject.GROSS_HEAT,
                        f"open {self.gross_heat:.0f} + {risk_amount:.0f} > "
                        f"{self.cfg.max_gross_heat * self.balance:.0f}")

        sign = self.usd_sign.get(signal.instrument, 0)
        contribution = signal.direction * sign * risk_amount
        projected = abs(self.net_usd_exposure + contribution)
        if projected > self.cfg.max_factor_exposure * self.balance:
            return deny(Reject.FACTOR_EXPOSURE,
                        f"net USD {self.net_usd_exposure:+.0f} -> {self.net_usd_exposure + contribution:+.0f} "
                        f"exceeds +/-{self.cfg.max_factor_exposure * self.balance:.0f}")

        # 6) size it: units so that (stop_pips * pip_value_per_unit) == risk_amount
        loss_per_unit = stop_pips * signal.pip_value_per_unit
        if loss_per_unit <= 0:
            return deny(Reject.BAD_SIZING, "non-positive loss_per_unit")
        units = int(risk_amount / loss_per_unit)
        if units <= 0:
            return deny(Reject.BAD_SIZING, "units rounded to 0 (risk too small / stop too wide)")

        note = ""
        if self.cfg.stop_atr_mult * signal.atr_pips < floor:
            note = f"stop at {floor:.0f}-pip floor"
        if self._consecutive_losses >= self.cfg.derisk_after_losses:
            note = (note + "; " if note else "") + \
                   f"de-risked x{self.cfg.derisk_factor} after {self._consecutive_losses} losses"

        return RiskDecision(
            approved=True,
            reason=Reject.OK,
            units=units * signal.direction,   # signed: OANDA buys with +, sells with -
            stop_price=round(stop_price, 5),
            stop_pips=round(stop_pips, 1),
            take_profit=round(take_profit, 5),
            risk_amount=round(risk_amount, 2),
            note=note,
        )


# --------------------------------------------------------------------------- #
# Demo / smoke test  (run:  python risk_manager.py)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    # SGD account, 10,000 balance. pip_value_per_unit is illustrative
    # (your execution layer computes the real number from live rates).
    PV_NONJPY = 0.00013   # ~SGD value of 1 pip per 1 unit for a *_USD pair
    PV_JPY    = 0.00009   # ~SGD value of 1 pip per 1 unit for USD_JPY

    rm = RiskManager(RiskConfig(), balance=10_000)

    def show(tag: str, sig: TradeSignal) -> None:
        d = rm.evaluate(sig)
        status = "APPROVED" if d.approved else f"REJECTED ({d.reason.value})"
        extra = f"  units={d.units:+d} stop={d.stop_price} ({d.stop_pips}p) risk={d.risk_amount}" if d.approved else ""
        note = f"  [{d.note}]" if d.note else (f"  [{ '' }]" if d.approved else "")
        print(f"\n{tag}\n  -> {status}{extra}{note.rstrip(' []') and ('  ' + d.note) if d.note else ''}")
        if d.approved:
            rm.register_fill(sig.instrument, sig.direction, d.risk_amount)
        print(f"     state: heat={rm.gross_heat:.0f}/{rm.cfg.max_gross_heat*rm.balance:.0f}  "
              f"netUSD={rm.net_usd_exposure:+.0f}  daily_room={rm.daily_room:.0f}  "
              f"open={len(rm.open_positions)}")

    # 1) clean long EUR/USD -> short USD
    show("EUR_USD long (entry 1.0850, stop 1.0838 = 12p)",
         TradeSignal("EUR_USD", +1, 1.0850, 1.0838, PV_NONJPY, atr_pips=8.0, spread_pips=0.4, setup="A"))

    # 2) AUD/USD long -> also short USD. Adds to the same USD bet.
    show("AUD_USD long (adds to short-USD exposure)",
         TradeSignal("AUD_USD", +1, 0.6600, 0.6588, PV_NONJPY, atr_pips=8.0, spread_pips=0.6, setup="A"))

    # 3) NZD/USD long -> would push net short-USD past the 2% factor cap: REJECTED
    show("NZD_USD long (correlated USD bet -> hits factor cap)",
         TradeSignal("NZD_USD", +1, 0.6020, 0.6010, PV_NONJPY, atr_pips=8.0, spread_pips=0.8, setup="A"))

    # 4) USD/JPY long -> LONG USD, offsets the net exposure: APPROVED
    show("USD_JPY long (long USD -> offsets, allowed)",
         TradeSignal("USD_JPY", +1, 156.40, 156.10, PV_JPY, atr_pips=20.0, spread_pips=0.7, setup="B"))

    # 5) wide spread -> skipped
    show("GBP_USD long but spread 2.1p (> cap)",
         TradeSignal("GBP_USD", +1, 1.2730, 1.2718, PV_NONJPY, atr_pips=8.0, spread_pips=2.1, setup="A"))

    # 6) simulate a losing day to trip the kill switch
    print("\n--- simulating closed losers to arm the daily kill switch ---")
    rm.close_position("EUR_USD", pnl=-150)
    rm.close_position("AUD_USD", pnl=-160)
    rm.close_position("USD_JPY", pnl=-95)   # cumulative ~ -4% of 10k -> over the 3% limit
    print(f"daily P&L={rm.daily_pnl:.0f}  halted={rm.halted}")

    show("USD_CHF long after limit hit",
         TradeSignal("USD_CHF", +1, 0.8900, 0.8888, PV_NONJPY, atr_pips=8.0, spread_pips=0.5, setup="A"))
