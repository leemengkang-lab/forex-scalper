# Forex Scalper — Runbook

## Migration safety net (2026-06-14)
- Old bot `Forexbot` committed to git and tagged `pre-scalper-migration-2026-06-14`.
- Full archive: `C:\Users\User\Forexbot-archive-2026-06-14.zip`.
- The old bot is NOT deleted. It is retired only after the new bot is proven live (plan Phase 9.5).

## Account facts (detected 2026-06-14, read-only AccountSummary)
- Reused creds: Forexbot/.env (copied to forex-scalper/.env, gitignored).
- `OANDA_ENVIRONMENT=practice`. Account `101-003-35599767-001`.
- **Account currency: SGD** — pip-value math (Phase 3) uses `*_SGD` crosses.
- Balance ~939.41 SGD at detection (down ~6% from ~1000 — incumbent is losing).
- **1 open position** on this practice account at detection (the incumbent's). Plan flattens it at cutover (Phase 9.4), NOT during the build, so we don't go dark while the replacement is built+validated.

## Platform note
- This Windows dev box fails OANDA TLS verification (AV/proxy intercept). Fix: `truststore.inject_into_ssl()` at startup (uses the OS cert store). `truststore` is a project dep; the live loop / broker bootstrap must call it on Windows. The GCP VM (Linux) does not need it but the call is harmless there.

## Open action items
- [ ] SSH to the GCP e2-micro VM and record whether `forex-bot.service` is currently trading (`systemctl status forex-bot`), and whether the live deployment uses a separate (live) account vs this practice one. Note the answer here.
