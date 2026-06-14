from forex_scalper.data import MarketState
from forex_scalper.pip_value import pip_value_per_unit, quote_ccy


def test_quote_ccy():
    assert quote_ccy("EUR_USD") == "USD"
    assert quote_ccy("USD_JPY") == "JPY"


def test_usd_account_usd_quoted_pair():
    assert abs(pip_value_per_unit("EUR_USD", "USD", lambda c: 1.0) - 0.0001) < 1e-9


def test_sgd_account_usd_quoted_pair_uses_usd_sgd():
    rates = {"USD": 1.35}
    pv = pip_value_per_unit("EUR_USD", "SGD", lambda c: rates[c])
    assert abs(pv - 0.0001 * 1.35) < 1e-9


def test_jpy_pair_uses_001_pip_and_quote_jpy():
    # account SGD, quote JPY, JPY->SGD rate
    jpy_sgd = (1 / 110) * 1.35
    pv = pip_value_per_unit("USD_JPY", "SGD", lambda c: jpy_sgd if c == "JPY" else None)
    assert abs(pv - 0.01 * jpy_sgd) < 1e-12


def test_quote_equals_account_factor_one():
    # account SGD, pair X_SGD -> factor 1
    pv = pip_value_per_unit(
        "USD_SGD",
        "SGD",
        lambda c: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    assert abs(pv - 0.0001) < 1e-9


# --- MarketState integration tests ------------------------------------------ #


def test_marketstate_returns_zero_until_account_ccy_set():
    m = MarketState()
    assert m.pip_value_per_unit("EUR_USD") == 0.0


def test_marketstate_explicit_override_takes_precedence():
    m = MarketState()
    m.set_pip_value("EUR_USD", 0.00013)
    assert m.pip_value_per_unit("EUR_USD") == 0.00013   # backtest path unchanged


def test_marketstate_computes_from_account_ccy_and_cross():
    m = MarketState()
    m.set_account_ccy("SGD")
    m.set_cross_rate("USD", 1.35)
    assert abs(m.pip_value_per_unit("EUR_USD") - 0.0001 * 1.35) < 1e-9


def test_marketstate_zero_when_cross_unknown():
    m = MarketState()
    m.set_account_ccy("SGD")
    assert m.pip_value_per_unit("USD_CHF") == 0.0   # CHF->SGD not set yet
