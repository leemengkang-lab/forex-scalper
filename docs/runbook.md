# Forex Scalper — Runbook

## Migration safety net (2026-06-14)
- Old bot `Forexbot` committed to git and tagged `pre-scalper-migration-2026-06-14`.
- Full archive: `C:\Users\User\Forexbot-archive-2026-06-14.zip`.
- The old bot is NOT deleted. It is retired only after the new bot is proven live (plan Phase 9.5).

## Open action items (manual, require GCP VM access)
- [ ] SSH to the GCP e2-micro VM and record whether `forex-bot.service` is currently trading (`systemctl status forex-bot`). Note the answer here.
- [ ] Confirm the OANDA account currency (pip-value math depends on it) and whether the live bot holds any open positions right now.
