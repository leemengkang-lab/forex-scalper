"""
Tests for forex_scalper.risk_manager: bias-agnostic sizing, ATR-based stop,
1.3x take-profit, and the minimum-volatility veto.
"""

from forex_scalper.risk_manager import Reject, RiskConfig, RiskManager, TradeSignal

PV = 0.00013  # illustrative pip value per unit (SGD, non-JPY pair)
ATR = 8.0     # 1M ATR in pips, comfortably above the 2.5 floor


def test_clean_long_is_approved_and_sized():
    rm = RiskManager(RiskConfig(), balance=10_000)
    d = rm.evaluate(TradeSignal("EUR_USD", +1, 1.0850, 1.0838, PV, atr_pips=ATR, spread_pips=0.4))
    assert d.approved and d.units > 0
    assert d.stop_pips == 12.0          # max(1.5*8, 10)


def test_stop_uses_atr_but_respects_floor():
    rm = RiskManager(RiskConfig(), balance=10_000)
    d = rm.evaluate(TradeSignal("EUR_USD", +1, 1.10, 1.099, PV, atr_pips=2.6, spread_pips=0.4))
    assert d.approved and d.stop_pips == 10.0   # 1.5*2.6=3.9 -> floored to 10


def test_take_profit_is_1_3x_stop_long():
    rm = RiskManager(RiskConfig(), balance=10_000)
    d = rm.evaluate(TradeSignal("EUR_USD", +1, 1.0850, 1.0838, PV, atr_pips=ATR, spread_pips=0.4))
    # stop 12 pips below entry; tp 1.3*12 = 15.6 pips above entry
    assert d.take_profit > 1.0850
    assert abs((d.take_profit - 1.0850) / 0.0001 - 1.3 * d.stop_pips) < 1e-6
    assert d.stop_price < 1.0850


def test_take_profit_is_1_3x_stop_short():
    rm = RiskManager(RiskConfig(), balance=10_000)
    d = rm.evaluate(TradeSignal("EUR_USD", -1, 1.0850, 1.0862, PV, atr_pips=ATR, spread_pips=0.4))
    assert d.take_profit < 1.0850       # tp below entry for a short
    assert d.stop_price > 1.0850        # stop above entry for a short
    assert abs((1.0850 - d.take_profit) / 0.0001 - 1.3 * d.stop_pips) < 1e-6


def test_low_volatility_rejected():
    rm = RiskManager(RiskConfig(), balance=10_000)
    d = rm.evaluate(TradeSignal("EUR_USD", +1, 1.0850, 1.0838, PV, atr_pips=2.0, spread_pips=0.4))
    assert not d.approved and d.reason is Reject.LOW_VOLATILITY


def test_zero_atr_rejected_as_low_volatility():
    rm = RiskManager(RiskConfig(), balance=10_000)
    d = rm.evaluate(TradeSignal("EUR_USD", +1, 1.0850, 1.0838, PV, atr_pips=0.0, spread_pips=0.4))
    assert not d.approved and d.reason is Reject.LOW_VOLATILITY


def test_wide_spread_rejected():
    rm = RiskManager(RiskConfig(), balance=10_000)
    d = rm.evaluate(TradeSignal("GBP_USD", +1, 1.2730, 1.2718, PV, atr_pips=ATR, spread_pips=2.1))
    assert not d.approved and d.reason is Reject.SPREAD


def test_factor_cap_blocks_third_correlated_usd_bet():
    rm = RiskManager(RiskConfig(), balance=10_000)
    for ins in ("EUR_USD", "AUD_USD"):
        d = rm.evaluate(TradeSignal(ins, +1, 1.0, 0.9988, PV, atr_pips=ATR, spread_pips=0.4))
        assert d.approved
        rm.register_fill(ins, +1, d.risk_amount)
    d = rm.evaluate(TradeSignal("NZD_USD", +1, 0.60, 0.5988, PV, atr_pips=ATR, spread_pips=0.4))
    assert not d.approved and d.reason is Reject.FACTOR_EXPOSURE


def test_daily_loss_limit_halts():
    rm = RiskManager(RiskConfig(), balance=10_000)
    rm.close_position("EUR_USD", pnl=-310)
    assert rm.halted
    d = rm.evaluate(TradeSignal("USD_CHF", +1, 0.89, 0.8888, PV, atr_pips=ATR, spread_pips=0.4))
    assert not d.approved and d.reason in (Reject.HALTED, Reject.DAILY_LIMIT)
