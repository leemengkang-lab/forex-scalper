#!/usr/bin/env bash
#
# deploy_vm.sh — replace the old forex-bot with forex-scalper on its VM (DEMO).
#
# Run it like this (the repo must already be cloned to /opt/forex-scalper):
#     sudo bash /opt/forex-scalper/scripts/deploy_vm.sh
#
# It reuses the demo creds already on the box (/opt/forex-bot/.env), stops the
# old bot, installs the new one, and starts it under systemd as YOUR user.
# Practice account only — zero real-money risk. (Strategy is unvalidated: this
# is observation, not a proven edge.)
set -euo pipefail

APP=/opt/forex-scalper
ENVFILE=/etc/forex-scalper.env
RUN_USER="${SUDO_USER:-root}"
OLD_ENV=/opt/forex-bot/.env

echo "[1/6] Stopping the old forex-bot (one bot per account/token)..."
sudo systemctl disable --now forex-bot 2>/dev/null && echo "  old forex-bot stopped." \
  || echo "  forex-bot not found / already stopped (continuing)."

echo "[2/6] Reusing the demo creds already on this VM..."
if [ ! -f "$ENVFILE" ]; then
  if [ -f "$OLD_ENV" ]; then
    sudo cp "$OLD_ENV" "$ENVFILE"
    echo "  copied creds from $OLD_ENV -> $ENVFILE"
  else
    echo "  ERROR: $ENVFILE missing and $OLD_ENV not found. Create $ENVFILE first." >&2
    exit 1
  fi
fi
sudo chmod 600 "$ENVFILE"

echo "[3/6] Building the Python environment + installing..."
sudo python3 -m venv "$APP/.venv"
sudo "$APP/.venv/bin/pip" install --upgrade pip -q
sudo "$APP/.venv/bin/pip" install -q "$APP"

echo "[4/6] Data dir + ownership (service runs as: $RUN_USER)..."
sudo mkdir -p "$APP/data"
sudo chown -R "$RUN_USER:$RUN_USER" "$APP"

echo "[5/6] Writing the systemd service..."
sudo tee /etc/systemd/system/forex-scalper.service >/dev/null <<UNIT
[Unit]
Description=forex-scalper live bot (DEMO / practice)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP
EnvironmentFile=$ENVFILE
ExecStart=$APP/.venv/bin/python -m forex_scalper.live --practice
Restart=always
RestartSec=10
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

echo "[6/6] Starting forex-scalper..."
sudo systemctl daemon-reload
sudo systemctl enable --now forex-scalper
sleep 4
sudo systemctl --no-pager status forex-scalper | head -n 18
echo
echo "============================================================"
echo "Done. The new bot is running on the DEMO account."
echo "  Live logs:        journalctl -u forex-scalper -f"
echo "  Telegram:         /status   /positions   (alerts on every fill)"
echo "  Trades fire only during 12:00-16:00 UTC (London/NY overlap)."
echo "  Roll back to old: sudo systemctl disable --now forex-scalper && sudo systemctl enable --now forex-bot"
echo "============================================================"
