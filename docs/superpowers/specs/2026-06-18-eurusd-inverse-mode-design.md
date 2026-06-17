# EUR/USD Inverse (Reversal) Mode — Design

**Date:** 2026-06-18
**Status:** Approved (design); pending implementation plan.

## Goal

Add an **inverse mode**: when the strategy detects an entry signal, take the
**opposite** side instead — buy becomes sell, sell becomes buy. Scoped to
**EUR_USD only**; the other four pairs trade normally. Built as a live-capable
config flag (it does **not** turn live trading on — `config.live` remains the
separate, explicit switch, and the Phase 8 real-data validation gate still
applies before real money).

## Honest caveat (recorded, not blocking)

Flipping a losing strategy does **not** automatically make it profitable. Every
trade pays the spread (and any commission/slippage) regardless of direction, so
a near-break-even-before-costs strategy loses to the spread on *both* sides when
inverted. Inversion only wins if the underlying signal carries *negative* edge
exceeding total costs. The user has chosen to build this for live use; this
caveat is recorded so the expectation is explicit. Backtesting both `[]` and
`["EUR_USD"]` is the way to see whether the flip actually has edge.

## Key technical finding

The inversion reduces to **negating `direction`** on the `SetupSignal`. No stop
mirroring is needed, because `risk_manager.evaluate` (risk_manager.py:184-188)
uses only the *magnitude* of the stop distance and **re-derives** the stop side
from `direction`:

```python
raw_pips   = abs(signal.entry_price - signal.stop_price) / psize   # side ignored
stop_pips  = max(raw_pips, floor)
stop_price = signal.entry_price - signal.direction * stop_pips * psize  # re-derived
```

Consequently, once `direction` is flipped:
- the stop is re-derived on the correct (opposite) side automatically,
- the take-profit in `bot._try_enter` flips (it multiplies by `direction`),
- `units` come out signed correctly,
- the USD factor-exposure sign flips,
- the journal, watchdog, and OANDA `stopLossOnFill`/`takeProfitOnFill` all see a
  normal, consistent trade.

## Architecture

Single chokepoint. Inversion is applied in `ScalpBot.on_candle_close`
(bot.py) **after** `setups.detect()` returns a signal and **before**
`_try_enter`. Because the backtest drives the bot through the same
`on_candle_close` (backtest.py:260), backtest, paper, and live share one code
path — what you validate is what you run.

A trade still only triggers when a *normal* setup would have fired (in-bias,
in-regime). Inversion changes the **side taken**, not the **trigger**. So on
EUR_USD the bot effectively trades against its own 1H bias; the bias/regime
gates and all risk caps remain in force.

### Components

1. **Config field** — `BotConfig.invert_instruments: list[str]`
   (default `[]` = off). Enable with `["EUR_USD"]`. Empty default keeps the
   "turn it on deliberately" convention used by `live`. Set wherever
   `BotConfig` is constructed (and in the relevant yaml if a loader exists —
   verify during implementation).

2. **Pure flip helper** — `maybe_invert(sig: SetupSignal, invert_instruments) -> SetupSignal`:
   - returns `sig` unchanged when `sig.instrument` not in the list,
   - otherwise returns `replace(sig, direction=-sig.direction, note="INVERTED: " + sig.note)`.
   - Pure, total, no failure modes. (Location: small new module `inversion.py`
     or a function in `setups.py` — decide in plan; `inversion.py` keeps setup
     detection clean.)

3. **Wiring** — one line in `on_candle_close`:
   ```python
   sig = setups_mod.detect(...)
   if sig is None:
       return
   sig = maybe_invert(sig, self.cfg.invert_instruments)
   self._try_enter(sig, bias, regime, now)
   ```

### Data flow

`detect → maybe_invert → _try_enter → risk.evaluate → broker.place_market_order`.
Only the `SetupSignal.direction` (and `note`) change; everything downstream is
untouched and already tested.

## Error handling

No new failure modes. The flip is pure; the existing `on_candle_close`
try/except still wraps the path.

## Testing

- **Unit (`maybe_invert`):** EUR_USD long→short with entry and stop distance
  preserved; AUD_USD untouched; no-op when list is empty; note is prefixed.
- **Integration (through `on_candle_close` + PaperBroker):** a detected EUR_USD
  long yields a SHORT order with stop **above** entry and TP **below**; an
  AUD_USD long stays long.
- **Backtest parity:** a short run with `["EUR_USD"]` vs `[]` shows EUR_USD
  trades reversed and the other four pairs identical.

## Out of scope

- Turning on live trading (`config.live`) — unchanged, separate switch.
- Inverting any pair other than EUR_USD (mechanism is a list, trivially
  extensible, but default and intent is EUR_USD only).
- Re-running / changing the Phase 8 validation gate (separate work).
