"""Guard: the backtest harness must instantiate and run end-to-end.

This caught a real regression: adding `modify_stop` to the Broker ABC left
BacktestBroker abstract (uninstantiable). Nothing else exercises the backtest
script, so this lightweight smoke test pins it.
"""
from forex_scalper.backtest import BacktestBroker, Backtester, synth
from forex_scalper.config import BotConfig


def test_backtest_broker_is_concrete():
    # Must not raise "Can't instantiate abstract class" — every Broker abstract
    # method (incl. modify_stop) must be implemented.
    b = BacktestBroker({"EUR_USD": 0.00013})
    assert b.modify_stop("nope", 1.0) is False


def test_backtester_runs_a_short_synthetic_session():
    cfg = BotConfig(starting_balance=10_000.0)
    bt = Backtester(cfg, "EUR_USD", 0.00013)
    candles = synth(days=2, instrument="EUR_USD")
    bt.run(candles)                      # must complete without error
    assert isinstance(bt.records, list)  # may be empty; the point is it ran
