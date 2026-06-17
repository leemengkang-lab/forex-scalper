from forex_scalper.inversion import maybe_invert
from forex_scalper.models import LONG, SHORT, SetupSignal


def _sig(direction=LONG, instrument="EUR_USD"):
    # SetupSignal(instrument, direction, entry_price, stop_price, setup, spread_pips, note)
    return SetupSignal(instrument, direction, 1.0850, 1.0838, "A", 0.4, "pullback")


def test_no_op_when_list_empty():
    s = _sig()
    assert maybe_invert(s, []) is s


def test_no_op_for_instrument_not_in_list():
    s = _sig(instrument="AUD_USD")
    assert maybe_invert(s, ["EUR_USD"]) is s


def test_flips_long_to_short_for_listed_instrument():
    s = _sig(LONG)
    out = maybe_invert(s, ["EUR_USD"])
    assert out.direction == SHORT
    assert out.instrument == "EUR_USD"
    assert out.entry_price == s.entry_price      # entry unchanged
    assert out.stop_price == s.stop_price        # distance preserved; risk re-derives side
    assert out.spread_pips == s.spread_pips
    assert out.setup == s.setup
    assert out.note.startswith("INVERTED:")


def test_flips_short_to_long():
    s = _sig(SHORT)
    out = maybe_invert(s, ["EUR_USD"])
    assert out.direction == LONG
