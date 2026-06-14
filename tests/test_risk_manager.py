"""
Characterization tests for forex_scalper.risk_manager.
These pin the CURRENT behavior — do not change source to make them pass.
"""

from forex_scalper.risk_manager import Reject, RiskConfig, RiskManager, TradeSignal

PV = 0.00013  # illustrative pip value per unit (SGD, non-JPY pair)


def test_clean_long_is_approved_and_sized():
    rm = RiskManager(RiskConfig(), balance=10_000)
    d = rm.evaluate(TradeSignal("EUR_USD", +1, 1.0850, 1.0838, PV, spread_pips=0.4))
    assert d.approved and d.units > 0 and d.stop_pips >= 10.0


def test_wide_spread_rejected():
    rm = RiskManager(RiskConfig(), balance=10_000)
    d = rm.evaluate(TradeSignal("GBP_USD", +1, 1.2730, 1.2718, PV, spread_pips=2.1))
    assert not d.approved and d.reason is Reject.SPREAD


def test_factor_cap_blocks_third_correlated_usd_bet():
    # EUR_USD long and AUD_USD long both add short-USD exposure.
    # A third NZD_USD long would push net short-USD past the 2% factor cap.
    rm = RiskManager(RiskConfig(), balance=10_000)
    for ins in ("EUR_USD", "AUD_USD"):
        d = rm.evaluate(TradeSignal(ins, +1, 1.0, 0.9988, PV, spread_pips=0.4))
        assert d.approved
        rm.register_fill(ins, +1, d.risk_amount)
    d = rm.evaluate(TradeSignal("NZD_USD", +1, 0.60, 0.5988, PV, spread_pips=0.4))
    assert not d.approved and d.reason is Reject.FACTOR_EXPOSURE


def test_daily_loss_limit_halts():
    # -310 on a 10k balance exceeds the 3% daily loss limit (-300)
    rm = RiskManager(RiskConfig(), balance=10_000)
    rm.close_position("EUR_USD", pnl=-310)
    assert rm.halted
    d = rm.evaluate(TradeSignal("USD_CHF", +1, 0.89, 0.8888, PV, spread_pips=0.4))
    assert not d.approved and d.reason in (Reject.HALTED, Reject.DAILY_LIMIT)
