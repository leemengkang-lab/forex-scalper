# Bias-Aligned, Correct-R:R Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove inversion, fix reward:risk structurally, filter dead markets, run Setup A alone bias-aligned, and journal every close — the first correct config for a real demo soak.

**Architecture:** Reward:risk becomes the single responsibility of `risk_manager` (stop = 1.5×ATR floored, TP = 1.3×stop). A minimum-volatility veto lives there too. Setup B is gated off by config. Closes are journalled at each close site, with an accurate reason threaded through a tiny shared `CloseReasons` registry consumed by the reconciler (the existing single close-processor).

**Tech Stack:** Python 3.13, dataclasses, pytest, ruff, mypy. Run commands use `.venv/Scripts/python`.

**Spec:** `docs/superpowers/specs/2026-06-18-bias-aligned-rr-fix-design.md`

## Global Constraints

- ONE rule set for all pairs — no per-pair overrides.
- `risk_manager` is the single source of risk truth (stop, size, TP, vetoes).
- Nothing trades real money: `OANDA_ENVIRONMENT` stays `practice`; `config.live` untouched.
- New knobs and defaults (all in `RiskConfig`): `stop_atr_mult=1.5`, `tp_r_multiple=1.3`, `min_atr_pips=2.5`. Floors unchanged: `min_stop_pips_default=10`, `min_stop_pips_jpy=15`.

---

## File Structure

- Delete: `src/forex_scalper/inversion.py`, `tests/test_inversion.py`, `tests/test_inverse_mode_bot.py`
- Create: `src/forex_scalper/close_reasons.py` (shared close-reason registry), `tests/test_close_reasons.py`
- Modify: `src/forex_scalper/config.py` (remove `invert_instruments` + `tp_atr_mult`)
- Modify: `src/forex_scalper/risk_manager.py` (atr_pips, R:R math, min-vol veto, take_profit)
- Modify: `src/forex_scalper/bot.py` (drop inversion, pass atr_pips, use decision.take_profit, bias guard)
- Modify: `src/forex_scalper/setups.py` (gate Setup B)
- Modify: `src/forex_scalper/trade_manager.py` (mark time_stop reason)
- Modify: `src/forex_scalper/reconcile.py` (journal closes with reason)
- Modify: `src/forex_scalper/live.py` (remove INVERT parsing; wire journal + close_reasons)
- Modify: `.env.example`, `docs/runbook.md`
- Modify tests: `tests/test_risk_manager.py` (supply atr_pips), `tests/test_reconcile.py` (unaffected — journal optional)

---

## Task 1: Remove inversion entirely

**Files:**
- Delete: `src/forex_scalper/inversion.py`, `tests/test_inversion.py`, `tests/test_inverse_mode_bot.py`
- Modify: `src/forex_scalper/config.py`, `src/forex_scalper/bot.py`, `src/forex_scalper/live.py`, `.env.example`

- [ ] **Step 1: Delete the inversion module and its tests**

```bash
git rm src/forex_scalper/inversion.py tests/test_inversion.py tests/test_inverse_mode_bot.py
```

- [ ] **Step 2: Remove the config field** — in `src/forex_scalper/config.py`, delete these three lines:

```python
    # Instruments whose detected setups are taken on the OPPOSITE side
    # (buy<->sell). Empty = off. Enable EUR/USD inverse mode with ["EUR_USD"].
    invert_instruments: list[str] = field(default_factory=list)
```

- [ ] **Step 3: Remove the wiring in `bot.py`** — delete the import line:

```python
from forex_scalper.inversion import maybe_invert
```

and delete the call line in `on_candle_close`:

```python
            sig = maybe_invert(sig, self.cfg.invert_instruments)  # opposite side if configured
```

- [ ] **Step 4: Remove the env parsing in `live.py`** — delete the import:

```python
from forex_scalper.inversion import parse_invert_instruments
```

and replace this block:

```python
    # INVERT_INSTRUMENTS (comma-separated, e.g. "EUR_USD") flips those pairs to
    # the opposite side. Empty/unset = normal trading. See docs spec
    # 2026-06-18-eurusd-inverse-mode-design.md.
    invert_instruments = parse_invert_instruments(os.environ.get("INVERT_INSTRUMENTS"))
    cfg = BotConfig(invert_instruments=invert_instruments)
    if invert_instruments:
        log.warning("INVERSE MODE active for: %s", ", ".join(invert_instruments))
    tradeable = cfg.instruments
```

with:

```python
    cfg = BotConfig()
    tradeable = cfg.instruments
```

- [ ] **Step 5: Remove the `.env.example` lines**

```
# Inverse mode: comma-separated pairs whose signals are taken on the OPPOSITE
# side (buy<->sell). Empty/unset = normal trading. Example: EUR_USD
INVERT_INSTRUMENTS=
```

- [ ] **Step 6: Verify the package imports and suite is green (minus deleted tests)**

Run: `.venv/Scripts/python -c "import forex_scalper.bot, forex_scalper.live, forex_scalper.config; print('ok')"`
Expected: prints `ok` (no `ModuleNotFoundError` for inversion).
Run: `.venv/Scripts/python -m pytest -q`
Expected: passes; `test_inversion`/`test_inverse_mode_bot` no longer collected.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "revert: remove EUR/USD inverse mode entirely"
```

---

## Task 2: Reward:risk + minimum-volatility in `risk_manager`, wired into `bot`

**Files:**
- Modify: `src/forex_scalper/risk_manager.py`
- Modify: `src/forex_scalper/config.py` (remove `tp_atr_mult`)
- Modify: `src/forex_scalper/bot.py` (`_try_enter`)
- Test: `tests/test_risk_manager.py`

**Interfaces:**
- Produces: `TradeSignal(..., atr_pips: float = 0.0, ...)`; `RiskConfig.stop_atr_mult=1.5`, `.tp_r_multiple=1.3`, `.min_atr_pips=2.5`; `Reject.LOW_VOLATILITY`; `RiskDecision.take_profit: float`.

- [ ] **Step 1: Write/adjust the failing tests** — replace the body of `tests/test_risk_manager.py` with:

```python
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
```

- [ ] **Step 2: Run — expect FAIL** (`TradeSignal` has no `atr_pips`; no `LOW_VOLATILITY`).

Run: `.venv/Scripts/python -m pytest tests/test_risk_manager.py -q`
Expected: FAIL (TypeError on `atr_pips` / AttributeError on `Reject.LOW_VOLATILITY`).

- [ ] **Step 3: Add config fields** — in `RiskConfig` (`src/forex_scalper/risk_manager.py`), add after `max_spread_pips`:

```python
    stop_atr_mult: float = 1.5          # stop distance = this * 1M ATR (pips)
    tp_r_multiple: float = 1.3          # take-profit = this * stop (>=1 -> paid >= risked)
    min_atr_pips: float = 2.5           # skip entries when 1M ATR is below this (too dead)
```

- [ ] **Step 4: Add `atr_pips` to `TradeSignal`** — insert after `pip_value_per_unit`:

```python
    atr_pips: float = 0.0      # 1M ATR in pips (caller computes); drives stop + min-vol veto
```

- [ ] **Step 5: Add the reject reason + decision field**

In `class Reject(Enum)` add:

```python
    LOW_VOLATILITY = "1M ATR below minimum; market too quiet to scalp"
```

In `class RiskDecision` add:

```python
    take_profit: float = 0.0
```

- [ ] **Step 6: Rewrite the stop/TP/veto in `evaluate`** — replace this block:

```python
        # 2) cheap gates
        if signal.spread_pips > self.cfg.max_spread_pips:
            return deny(Reject.SPREAD, f"spread {signal.spread_pips:.2f} > {self.cfg.max_spread_pips}")
        if any(p.instrument == signal.instrument for p in self.open_positions):
            return deny(Reject.DUPLICATE)
        if len(self.open_positions) >= self.cfg.max_concurrent_positions:
            return deny(Reject.MAX_POSITIONS)

        # 3) stop distance with the minimum floor (widen if too tight)
        psize = pip_size(signal.instrument)
        floor = self.cfg.min_stop_pips_jpy if signal.instrument.upper().endswith("JPY") \
            else self.cfg.min_stop_pips_default
        raw_pips = abs(signal.entry_price - signal.stop_price) / psize
        stop_pips = max(raw_pips, floor)
        stop_price = signal.entry_price - signal.direction * stop_pips * psize
```

with:

```python
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
```

- [ ] **Step 7: Fix the widen-note + return** — replace this block:

```python
        note = ""
        if stop_pips > raw_pips:
            note = f"stop widened to {floor:.0f}-pip floor"
        if self._consecutive_losses >= self.cfg.derisk_after_losses:
            note = (note + "; " if note else "") + \
                   f"de-risked x{self.cfg.derisk_factor} after {self._consecutive_losses} losses"

        return RiskDecision(
            approved=True,
            reason=Reject.OK,
            units=units * signal.direction,   # signed: OANDA buys with +, sells with -
            stop_price=round(stop_price, 5),
            stop_pips=round(stop_pips, 1),
            risk_amount=round(risk_amount, 2),
            note=note,
        )
```

with:

```python
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
```

- [ ] **Step 8: Run risk tests — expect PASS**

Run: `.venv/Scripts/python -m pytest tests/test_risk_manager.py -q`
Expected: PASS.

- [ ] **Step 9: Wire `bot._try_enter`** — in `src/forex_scalper/bot.py`, add `atr_pips=atr_pips,` to the `TradeSignal(...)` construction:

```python
        tsig = TradeSignal(
            instrument=sig.instrument, direction=sig.direction,
            entry_price=sig.entry_price, stop_price=sig.stop_price,
            pip_value_per_unit=pv, atr_pips=atr_pips,
            spread_pips=sig.spread_pips, setup=sig.setup,
        )
```

Then replace the TP block:

```python
        # take-profit from ATR (your 2.5x finding), in the trade direction
        pip = pip_size(sig.instrument)
        tp = sig.entry_price + sig.direction * self.cfg.tp_atr_mult * (atr or 0.0)

        trade = self.broker.place_market_order(
            sig.instrument, decision.units, decision.stop_price, round(tp, 5), sig.setup)
```

with:

```python
        tp = decision.take_profit          # risk_manager owns TP (1.3x the stop)

        trade = self.broker.place_market_order(
            sig.instrument, decision.units, decision.stop_price, round(tp, 5), sig.setup)
```

- [ ] **Step 10: Remove `tp_atr_mult` from `BotConfig`** — in `src/forex_scalper/config.py`, delete:

```python
    tp_atr_mult: float = 2.5         # take-profit distance in ATR (your finding)
```

- [ ] **Step 11: Run the full suite; fix entry-path fallout**

Run: `.venv/Scripts/python -m pytest -q`
Expected: PASS. If any entry-path/backtest test now rejects with `LOW_VOLATILITY` because its synthetic 1M ATR is under 2.5 pips, fix it by constructing that test's `BotConfig` with a risk override, e.g. `BotConfig(risk=RiskConfig(min_atr_pips=0.0))`, so the test still exercises entries. Do NOT lower the production default.

- [ ] **Step 12: Commit**

```bash
git add -A
git commit -m "feat(risk): ATR-based stop + 1.3x TP + min-volatility veto (single source of R:R)"
```

---

## Task 3: Strict bias-alignment guard

**Files:**
- Modify: `src/forex_scalper/bot.py`
- Test: `tests/test_bias_guard.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_bias_guard.py`:

```python
"""A signal whose direction contradicts a non-FLAT bias must be rejected."""

from datetime import UTC, datetime

from forex_scalper.bot import ScalpBot
from forex_scalper.config import BotConfig
from forex_scalper.demo_smoke import INSTR, PV, build_market
from forex_scalper.execution import PaperBroker
from forex_scalper.models import SHORT, Bias, Regime, SetupSignal


def test_short_signal_under_long_bias_is_rejected(tmp_path):
    market = build_market()
    cfg = BotConfig(journal_path=str(tmp_path / "j.csv"))
    broker = PaperBroker(pip_value={INSTR: PV})
    bid, ask = market._bid[INSTR], market._ask[INSTR]
    broker.set_price(INSTR, bid, ask)
    bot = ScalpBot(cfg, market, broker)

    # Hand _try_enter a SHORT while bias says LONG_ONLY -> must not open.
    sig = SetupSignal(INSTR, SHORT, bid, bid + 0.0012, "A", 0.4, "synthetic")
    bot._try_enter(sig, Bias.LONG_ONLY, Regime.TREND, datetime(2025, 1, 6, 13, 30, tzinfo=UTC))

    assert broker.open_trades() == []
```

- [ ] **Step 2: Run — expect FAIL** (a trade opens). Run: `.venv/Scripts/python -m pytest tests/test_bias_guard.py -q`

- [ ] **Step 3: Add the guard + import** — in `src/forex_scalper/bot.py`, change the models import:

```python
from forex_scalper.models import SetupSignal, pip_size
```

to:

```python
from forex_scalper.models import Bias, SetupSignal, pip_size
```

Then in `_try_enter`, immediately after the `base = JournalRow(...)` construction and before the `if not decision.approved:` check, insert:

```python
        # strict bias alignment (belt-and-suspenders; inversion removed, Setup B off)
        if (bias == Bias.LONG_ONLY and sig.direction < 0) or \
           (bias == Bias.SHORT_ONLY and sig.direction > 0):
            base.event = "reject"; base.exit_reason = "bias_mismatch"
            self.journal.log(base)
            return
```

- [ ] **Step 4: Run — expect PASS.** Run: `.venv/Scripts/python -m pytest tests/test_bias_guard.py -q`

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(bot): reject signals that contradict a non-FLAT bias"
```

---

## Task 4: Disable Setup B

**Files:**
- Modify: `src/forex_scalper/setups.py`
- Test: `tests/test_setup_b_disabled.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_setup_b_disabled.py`:

```python
"""Setup B (level rejection) only fires when enable_setup_b is True."""

from forex_scalper import setups as setups_mod
from forex_scalper.data import MarketState
from forex_scalper.models import Bias, Candle, Regime
from forex_scalper.setups import SetupConfig
from datetime import UTC, datetime


def _range_market():
    """A false-break-above-then-close-inside at the 1.1000 round number."""
    m = MarketState()
    t0 = datetime(2025, 1, 6, 12, 0, tzinfo=UTC)
    # enough 1M candles for ATR; last one false-breaks 1.1000 and closes back inside
    for i in range(20):
        m.add_candle("EUR_USD", "1M", Candle(t0, 1.0990, 1.0995, 1.0985, 1.0990))
    m.add_candle("EUR_USD", "1M", Candle(t0, 1.0996, 1.1006, 1.0994, 1.0997))
    m.set_price("EUR_USD", 1.0997, 1.0998)
    return m


def test_setup_b_suppressed_when_disabled():
    m = _range_market()
    cfg = SetupConfig(enable_setup_b=False)
    sig = setups_mod.detect(m, "EUR_USD", Bias.FLAT, Regime.RANGE, cfg)
    assert sig is None


def test_setup_b_fires_when_enabled():
    m = _range_market()
    cfg = SetupConfig(enable_setup_b=True)
    sig = setups_mod.detect(m, "EUR_USD", Bias.FLAT, Regime.RANGE, cfg)
    assert sig is not None and sig.setup == "B"
```

- [ ] **Step 2: Run — expect FAIL** (`SetupConfig` has no `enable_setup_b`; first test may already pass, second must fail). Run: `.venv/Scripts/python -m pytest tests/test_setup_b_disabled.py -q`

- [ ] **Step 3: Add the flag + gate** — in `src/forex_scalper/setups.py`, add to `SetupConfig`:

```python
    enable_setup_b: bool = False       # round-number fade; off until independently proven
```

Then change `detect`:

```python
    if regime == Regime.RANGE:
        sig = _level_rejection(market, instrument, bias, cfg)
        if sig:
            return sig
```

to:

```python
    if cfg.enable_setup_b and regime == Regime.RANGE:
        sig = _level_rejection(market, instrument, bias, cfg)
        if sig:
            return sig
```

- [ ] **Step 4: Run — expect PASS.** Run: `.venv/Scripts/python -m pytest tests/test_setup_b_disabled.py -q`

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(setups): gate Setup B behind enable_setup_b (default off)"
```

---

## Task 5: Journal every close with an accurate reason

**Files:**
- Create: `src/forex_scalper/close_reasons.py`
- Modify: `src/forex_scalper/reconcile.py`, `src/forex_scalper/trade_manager.py`, `src/forex_scalper/bot.py`, `src/forex_scalper/live.py`
- Test: `tests/test_close_reasons.py`, `tests/test_reconcile.py` (add one)

**Interfaces:**
- Produces: `CloseReasons.mark(trade_id: str, reason: str)`, `CloseReasons.take(trade_id: str, default: str) -> str`.
- `PositionReconciler(..., journal=None, close_reasons=None)`; when `journal` is set, `_close_one` writes a `JournalRow(event="close", ...)`.
- `TradeManager(cfg, broker, close_reasons=None)`; `_watchdog(..., close_reasons=None)`.

- [ ] **Step 1: Write the registry test** — create `tests/test_close_reasons.py`:

```python
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
```

- [ ] **Step 2: Run — expect FAIL** (no module). Run: `.venv/Scripts/python -m pytest tests/test_close_reasons.py -q`

- [ ] **Step 3: Implement `close_reasons.py`** — create `src/forex_scalper/close_reasons.py`:

```python
"""
close_reasons.py
================
Tiny shared registry so the site that CLOSES a trade (time stop, watchdog) can
tell the reconciler — which is the single place that records/journals closes —
WHY it closed. Unmarked trades default to the reconciler's reason (broker SL/TP).
"""

from __future__ import annotations


class CloseReasons:
    def __init__(self) -> None:
        self._reasons: dict[str, str] = {}

    def mark(self, trade_id: str, reason: str) -> None:
        self._reasons[trade_id] = reason

    def take(self, trade_id: str, default: str) -> str:
        """Return and consume the reason for trade_id, or `default` if unmarked."""
        return self._reasons.pop(trade_id, default)
```

- [ ] **Step 4: Run — expect PASS.** Run: `.venv/Scripts/python -m pytest tests/test_close_reasons.py -q`

- [ ] **Step 5: Write the reconciler-journals-closes test** — append to `tests/test_reconcile.py`:

```python
def test_sync_journals_close_with_reason(tmp_path):
    from datetime import UTC, datetime

    from forex_scalper.close_reasons import CloseReasons
    from forex_scalper.journal import Journal
    from forex_scalper.reconcile import PositionReconciler

    class _Repo:
        def __init__(self):
            self._open = [{"trade_id": "T1", "instrument": "EUR_USD", "risk_amount": 100.0}]
        def open_trades(self): return list(self._open)
        def record_close(self, *a, **k): self._open = []
        def is_halted(self): return False
        def set_halt(self, reason): pass

    class _Risk:
        halted = False
        open_positions: list = []
        def close_position(self, *a, **k): pass
        def register_fill(self, *a, **k): pass

    class _Broker:
        def open_trades(self): return []      # T1 closed at broker

    jrnl = Journal(str(tmp_path / "j.csv"))
    cr = CloseReasons()
    cr.mark("T1", "time_stop")
    rec = PositionReconciler(_Repo(), _Risk(), get_closed_pnl=lambda _t: -7.5,
                             journal=jrnl, close_reasons=cr)
    rec.sync(_Broker(), now=datetime(2025, 1, 6, tzinfo=UTC))

    rows = list(__import__("csv").DictReader(open(str(tmp_path / "j.csv"))))
    close_rows = [r for r in rows if r["event"] == "close"]
    assert len(close_rows) == 1
    assert close_rows[0]["instrument"] == "EUR_USD"
    assert close_rows[0]["exit_reason"] == "time_stop"
    assert float(close_rows[0]["pnl"]) == -7.5
```

- [ ] **Step 6: Run — expect FAIL** (`PositionReconciler` has no `journal`/`close_reasons`). Run: `.venv/Scripts/python -m pytest tests/test_reconcile.py::test_sync_journals_close_with_reason -q`

- [ ] **Step 7: Update the reconciler** — in `src/forex_scalper/reconcile.py`:

Add the import near the top:

```python
from forex_scalper.journal import JournalRow
```

Extend `__init__` (add two optional params + store them):

```python
    def __init__(
        self,
        repo: _Repo,
        risk: _Risk,
        *,
        get_closed_pnl: GetClosedPnl,
        now_utc: Callable[[], datetime] = lambda: datetime.now(UTC),
        journal: Any | None = None,
        close_reasons: Any | None = None,
    ) -> None:
        self._repo = repo
        self._risk = risk
        self._get_closed_pnl = get_closed_pnl
        self._now_utc = now_utc
        self._journal = journal
        self._close_reasons = close_reasons
```

Replace `_close_one` with a reason-aware, journalling version:

```python
    def _close_one(
        self, trade_id: str, instrument: str, now: datetime, default_reason: str = "broker_close"
    ) -> None:
        """Fetch P&L (with fallback), route to risk, persist + journal the close."""
        pnl = self._get_closed_pnl(trade_id)
        if pnl is None:
            logger.warning(
                "reconcile: get_closed_pnl returned None for trade_id=%s; falling back to pnl=0.0",
                trade_id,
            )
            pnl = 0.0

        reason = (
            self._close_reasons.take(trade_id, default_reason)
            if self._close_reasons is not None
            else default_reason
        )

        self._risk.close_position(instrument, pnl, now=now)
        self._repo.record_close(
            trade_id,
            exit_price=0.0,
            pnl=pnl,
            reason=reason,
            closed_at=now,
        )

        if self._journal is not None:
            self._journal.log(
                JournalRow(
                    event="close",
                    instrument=instrument,
                    pnl=round(pnl, 2),
                    exit=0.0,
                    exit_reason=reason,
                )
            )

        if self._risk.halted and not self._repo.is_halted():
            self._repo.set_halt("daily loss limit reached")
```

In `reconcile_on_startup`, change the orphan-in-db close call to pass the startup reason:

```python
        for tid in db_ids - broker_ids:
            row = db_by_id[tid]
            self._close_one(tid, row["instrument"], now, default_reason="broker_close_startup")
            summary.closed.append(tid)
```

(The `sync` call to `_close_one` keeps the default `"broker_close"`.)

- [ ] **Step 8: Run — expect PASS.** Run: `.venv/Scripts/python -m pytest tests/test_reconcile.py -q`

- [ ] **Step 9: Mark time-stop closes** — in `src/forex_scalper/trade_manager.py`, change the constructor:

```python
    def __init__(self, cfg: TradeManagerConfig, broker: Broker):
        self.cfg = cfg
        self.broker = broker
```

to:

```python
    def __init__(self, cfg: TradeManagerConfig, broker: Broker, close_reasons: object | None = None):
        self.cfg = cfg
        self.broker = broker
        self.close_reasons = close_reasons
```

and in `_manage_one`, mark before the time-stop close:

```python
        if age_min >= self.cfg.max_minutes and gain_pips < self.cfg.min_progress_pips:
            logger.info("Time stop %s (%.1f min, %.1f pips)", t.trade_id, age_min, gain_pips)
            if self.close_reasons is not None:
                self.close_reasons.mark(t.trade_id, "time_stop")
            self.broker.close_trade(t.trade_id)
            return
```

- [ ] **Step 10: Wire the registry through `ScalpBot`** — in `src/forex_scalper/bot.py` `__init__`, add the import at the top:

```python
from forex_scalper.close_reasons import CloseReasons
```

and change:

```python
        self.trades = TradeManager(cfg.trade, broker)
```

to:

```python
        self.close_reasons = CloseReasons()
        self.trades = TradeManager(cfg.trade, broker, close_reasons=self.close_reasons)
```

- [ ] **Step 11: Mark watchdog closes + wire live** — in `src/forex_scalper/live.py`:

Change the watchdog signature:

```python
async def _watchdog(
    broker: Any,
    notifier: Notifier,
    *,
    interval: float = 60.0,
) -> None:
```

to:

```python
async def _watchdog(
    broker: Any,
    notifier: Notifier,
    *,
    interval: float = 60.0,
    close_reasons: Any | None = None,
) -> None:
```

and mark before the watchdog close:

```python
                if t.stop_price is None or t.stop_price <= 0 or math.isnan(t.stop_price):
                    if close_reasons is not None:
                        close_reasons.mark(t.trade_id, "watchdog_no_stop")
                    broker.close_trade(t.trade_id)
```

Wire the reconciler build (in `run_live`) to pass the journal + registry:

```python
    reconciler = reconciler or PositionReconciler(
        repo, bot.risk, get_closed_pnl=get_closed_pnl,
        journal=bot.journal, close_reasons=bot.close_reasons,
    )
```

Wire the watchdog task creation:

```python
        asyncio.create_task(
            _watchdog(broker, notifier, interval=watchdog_interval,
                      close_reasons=bot.close_reasons), name="watchdog"
        ),
```

- [ ] **Step 12: Run the full suite — expect PASS.** Run: `.venv/Scripts/python -m pytest -q`

- [ ] **Step 13: Commit**

```bash
git add -A
git commit -m "feat(journal): record every close with pnl + accurate exit_reason"
```

---

## Task 6: Docs + full verification

**Files:**
- Modify: `docs/runbook.md`
- Verify only: whole repo

- [ ] **Step 1: Replace the runbook inverse-mode section** — in `docs/runbook.md`, delete the entire `## Inverse (reversal) mode — EUR/USD (added 2026-06-18)` section (through its "Turn it off" / trading-window note) and replace with:

```markdown
## Bias-aligned baseline (added 2026-06-18)

Inverse mode was removed. The bot now runs Setup A only, strictly bias-aligned,
with a corrected reward:risk and a minimum-volatility filter:

- Stop = `max(1.5 * 1M-ATR, floor)` (floor 10 pips non-JPY / 15 JPY).
- Take-profit = `1.3 * stop` — always paid more than risked.
- No trade when 1M ATR < 2.5 pips (`RiskConfig.min_atr_pips`).
- Setup B (round-number fade) is disabled (`SetupConfig.enable_setup_b=False`).
- Every close is journalled with pnl + exit_reason
  (time_stop / watchdog_no_stop / broker_close).

Knobs live in `RiskConfig` (`stop_atr_mult`, `tp_r_multiple`, `min_atr_pips`) and
`SetupConfig.enable_setup_b`. Deploy: pull `buildout`, `sudo bash
/opt/forex-scalper/scripts/deploy_vm.sh` (now restarts on redeploy). Verify:
`journalctl -u forex-scalper -n 60 --no-pager | grep -iE "live runner started|reconciled"`.
Let it run 2–3 weeks, then analyse `journal.csv` (opens + closes with pnl).
```

- [ ] **Step 2: Full suite + lint + types**

Run:
```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check src/forex_scalper tests
.venv/Scripts/python -m mypy src/forex_scalper
```
Expected: all pass, no errors. Fix any ruff/mypy issues inline (e.g. unused imports left by the inversion removal).

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "docs(runbook): replace inverse-mode section with bias-aligned baseline"
```

---

## Self-Review notes

- **Spec coverage:** remove inversion (Task 1) + defensive bias guard (Task 3); R:R math in risk_manager (Task 2); min-vol filter (Task 2); disable Setup B (Task 4); journal every close with accurate reason (Task 5); docs + verification (Task 6). All five spec goals mapped.
- **Type consistency:** `TradeSignal.atr_pips`, `RiskConfig.{stop_atr_mult,tp_r_multiple,min_atr_pips}`, `Reject.LOW_VOLATILITY`, `RiskDecision.take_profit` are defined in Task 2 and consumed by `bot._try_enter` (Task 2) and tests. `CloseReasons.mark/take` defined in Task 5 and consumed by trade_manager/watchdog/reconciler in the same task. `PositionReconciler` new kwargs are optional, so existing `tests/test_reconcile.py` constructions stay valid.
- **Known limitation (acceptable):** in paper/backtest the reconciler's close pnl comes from `get_closed_pnl`, which `PaperBroker` doesn't implement → pnl 0.0 there; live (OANDA) yields real `realizedPL`. Matches existing DB behavior; tests inject a `get_closed_pnl` to assert pnl flows.
