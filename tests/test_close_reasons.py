from forex_scalper.close_reasons import CloseReasons


def test_take_returns_default_when_unmarked():
    cr = CloseReasons()
    assert cr.take("T1", "broker_close") == "broker_close"


def test_mark_then_take_returns_marked_and_consumes():
    cr = CloseReasons()
    cr.mark("T1", "time_stop")
    assert cr.take("T1", "broker_close") == "time_stop"
    # consumed: a second take falls back to the default
    assert cr.take("T1", "broker_close") == "broker_close"
