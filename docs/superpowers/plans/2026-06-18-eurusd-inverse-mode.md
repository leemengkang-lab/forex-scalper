# EUR/USD Inverse (Reversal) Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a config-gated "inverse mode" that takes the opposite side of every detected setup for EUR_USD only (buy↔sell), leaving the other four pairs unchanged.

**Architecture:** A pure helper `maybe_invert` negates `SetupSignal.direction` for instruments listed in `BotConfig.invert_instruments`. It is applied at the single chokepoint in `ScalpBot.on_candle_close`, right after `setups.detect()` and before `_try_enter`. The risk manager already re-derives the stop side from `direction` and a magnitude-only distance, and the take-profit is computed from `direction`, so negating `direction` is sufficient — no stop mirroring. Because the backtest drives the bot through the same `on_candle_close`, backtest/paper/live share one code path.

**Tech Stack:** Python 3.13, dataclasses, pytest, ruff, mypy. Run commands use `.venv/Scripts/python`.

**Spec:** `docs/superpowers/specs/2026-06-18-eurusd-inverse-mode-design.md`

---

## File Structure

- Create: `src/forex_scalper/inversion.py` — the pure `maybe_invert` helper (one responsibility: flip side by policy).
- Create: `tests/test_inversion.py` — unit tests for `maybe_invert`.
- Create: `tests/test_inverse_mode_bot.py` — integration test driving `ScalpBot.on_candle_close` end-to-end with `PaperBroker`.
- Modify: `src/forex_scalper/config.py` — add `invert_instruments` field to `BotConfig`.
- Modify: `src/forex_scalper/bot.py` — import + one wiring line in `on_candle_close`.

---

## Task 1: Config flag `invert_instruments`

**Files:**
- Modify: `src/forex_scalper/config.py`
- Test: `tests/test_inverse_mode_bot.py` (new file; first test pins the default)

- [ ] **Step 1: Write the failing test** — create `tests/test_inverse_mode_bot.py`:

```python
"""
Integration tests for EUR/USD inverse mode through the orchestrator.
Reuses demo_smoke.build_market() which yields a Setup A LONG on EUR_USD.
"""

from datetime import UTC, datetime

from forex_scalper.bot import ScalpBot
from forex_scalper.config import BotConfig
from forex_scalper.demo_smoke import INSTR, PV, build_market
from forex_scalper.execution import PaperBroker


def test_invert_instruments_defaults_to_empty():
    assert BotConfig().invert_instruments == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_inverse_mode_bot.py -v`
Expected: FAIL with `AttributeError: 'BotConfig' object has no attribute 'invert_instruments'`.

- [ ] **Step 3: Add the field** — in `src/forex_scalper/config.py`, inside `BotConfig`, add after the `live: bool = False` line:

```python
    # Instruments whose detected setups are taken on the OPPOSITE side
    # (buy<->sell). Empty = off. Enable EUR/USD inverse mode with ["EUR_USD"].
    invert_instruments: list[str] = field(default_factory=list)
```

(`field` is already imported at the top of `config.py`.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_inverse_mode_bot.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forex_scalper/config.py tests/test_inverse_mode_bot.py
git commit -m "feat(config): add invert_instruments flag (default off)"
```

---

## Task 2: Pure `maybe_invert` helper

**Files:**
- Create: `src/forex_scalper/inversion.py`
- Test: `tests/test_inversion.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_inversion.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_inversion.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forex_scalper.inversion'`.

- [ ] **Step 3: Implement** — create `src/forex_scalper/inversion.py`:

```python
"""
inversion.py
============
Execution-policy helper: optionally take the OPPOSITE side of a detected setup.

When an instrument is listed in `invert_instruments`, we flip the signal's
direction (buy<->sell). Only `direction` (and `note`) change: the risk manager
re-derives the stop from direction + magnitude-only distance, and the bot's
take-profit is computed from direction, so entry_price and stop_price are left
untouched. Pure and total — no failure modes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from forex_scalper.models import SetupSignal


def maybe_invert(sig: SetupSignal, invert_instruments: Sequence[str]) -> SetupSignal:
    """Return an opposite-side copy of `sig` if its instrument is inverted, else `sig`."""
    if sig.instrument not in invert_instruments:
        return sig
    return replace(sig, direction=-sig.direction, note=f"INVERTED: {sig.note}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_inversion.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/forex_scalper/inversion.py tests/test_inversion.py
git commit -m "feat(inversion): pure maybe_invert helper to flip setup side"
```

---

## Task 3: Wire inversion into the orchestrator

**Files:**
- Modify: `src/forex_scalper/bot.py`
- Test: `tests/test_inverse_mode_bot.py` (add the end-to-end cases)

- [ ] **Step 1: Write the failing integration tests** — append to `tests/test_inverse_mode_bot.py`:

```python
def _run(invert_instruments, tmp_path):
    market = build_market()
    cfg = BotConfig(
        starting_balance=10_000.0,
        invert_instruments=invert_instruments,
        journal_path=str(tmp_path / "journal.csv"),
    )
    broker = PaperBroker(pip_value={INSTR: PV})
    bid, ask = market._bid[INSTR], market._ask[INSTR]
    broker.set_price(INSTR, bid, ask)
    bot = ScalpBot(cfg, market, broker)
    # time inside the London/NY overlap so the session gate passes
    now = datetime(2025, 1, 6, 13, 30, tzinfo=UTC)
    bot.on_candle_close(INSTR, now)
    return broker.open_trades()


def test_normal_mode_opens_long_on_eurusd(tmp_path):
    trades = _run([], tmp_path)
    assert len(trades) == 1
    t = trades[0]
    assert t.units > 0                       # LONG
    assert t.stop_price < t.entry_price      # stop below entry for a long
    assert t.take_profit > t.entry_price     # tp above entry for a long


def test_inverse_mode_opens_short_on_eurusd(tmp_path):
    trades = _run(["EUR_USD"], tmp_path)
    assert len(trades) == 1
    t = trades[0]
    assert t.units < 0                       # SHORT (flipped)
    assert t.stop_price > t.entry_price      # stop above entry for a short
    assert t.take_profit < t.entry_price     # tp below entry for a short
```

- [ ] **Step 2: Run to verify the inverse case fails**

Run: `.venv/Scripts/python -m pytest tests/test_inverse_mode_bot.py -v`
Expected: `test_normal_mode_opens_long_on_eurusd` PASSES (current behavior), `test_inverse_mode_opens_short_on_eurusd` FAILS (still opens a long — inversion not wired yet).

- [ ] **Step 3: Wire it in** — in `src/forex_scalper/bot.py`:

Add the import alongside the other `from forex_scalper import ...` imports near the top:

```python
from forex_scalper.inversion import maybe_invert
```

Then in `on_candle_close`, change the detect block from:

```python
            sig = setups_mod.detect(self.market, instrument, bias, regime, self.cfg.setup)  # 5)
            if sig is None:
                return

            self._try_enter(sig, bias, regime, now)      # 6-7) risk + execute
```

to:

```python
            sig = setups_mod.detect(self.market, instrument, bias, regime, self.cfg.setup)  # 5)
            if sig is None:
                return
            sig = maybe_invert(sig, self.cfg.invert_instruments)  # opposite side if configured

            self._try_enter(sig, bias, regime, now)      # 6-7) risk + execute
```

- [ ] **Step 4: Run to verify both pass**

Run: `.venv/Scripts/python -m pytest tests/test_inverse_mode_bot.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/forex_scalper/bot.py tests/test_inverse_mode_bot.py
git commit -m "feat(bot): apply maybe_invert at the on_candle_close chokepoint"
```

---

## Task 4: Full suite, lint, type-check

**Files:** none (verification only).

- [ ] **Step 1: Run the whole test suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all tests pass (the new tests plus the existing suite — inversion is off by default, so existing behavior is unchanged).

- [ ] **Step 2: ruff + mypy on the changed/added files**

Run:
```bash
.venv/Scripts/python -m ruff check src/forex_scalper/inversion.py src/forex_scalper/config.py src/forex_scalper/bot.py tests/test_inversion.py tests/test_inverse_mode_bot.py
.venv/Scripts/python -m mypy src/forex_scalper/inversion.py src/forex_scalper/config.py src/forex_scalper/bot.py
```
Expected: no errors.

- [ ] **Step 3: Commit any lint/type fixups (if needed)**

```bash
git add -A
git commit -m "chore: ruff/mypy clean for inverse mode"
```

---

## How to enable (operational note, not a build step)

Inverse mode is **off by default**. To run EUR/USD inverted, construct `BotConfig(invert_instruments=["EUR_USD"])` wherever the bot is wired (and set it in the relevant `config/*.yaml` if/when a yaml loader populates `BotConfig`). This is independent of `config.live`; real-money trading still requires `live=True` and the Phase 8 validation gate.

---

## Self-Review notes

- **Spec coverage:** config flag (Task 1), pure `maybe_invert` with entry/stop untouched (Task 2), single-chokepoint wiring shared by backtest/paper/live (Task 3), test trio — unit + normal + inverse integration (Tasks 2–3), full-suite/lint/type gate (Task 4). The "honest caveat" and "out of scope (live, other pairs, Phase 8)" items in the spec require no code.
- **Type consistency:** `maybe_invert(sig: SetupSignal, invert_instruments: Sequence[str]) -> SetupSignal` is used identically in Task 2 (tests) and Task 3 (call site passes `self.cfg.invert_instruments`, a `list[str]`). `BotConfig.invert_instruments` defined in Task 1 matches.
- **No placeholders:** every step has concrete code/commands and expected output.
