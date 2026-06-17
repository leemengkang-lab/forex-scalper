#!/usr/bin/env bash
#
# deploy_vm.sh — replace the old forex-bot with forex-scalper on its VM (DEMO).
#
#   sudo bash /opt/forex-scalper/scripts/deploy_vm.sh
#
# Reuses the demo creds already on the box (/opt/forex-bot/.env), installs
# Python 3.13 if needed, stops the old bot, and runs the new one under systemd
# as a dedicated locked-down service account. Practice account only — $0 risk.
#
# Security model:
#   - source tree + venv: root-owned, read-only to the service account
#   - runtime state (db, journal): a separate dir owned by the service account
#   - service runs as a dedicated `botuser` (never root, never the sudoer)
#   - systemd hardening: ProtectSystem=strict, ProtectHome, PrivateTmp, etc.
set -euo pipefail

APP=/opt/forex-scalper
STATE=/var/lib/forex-scalper
ENVFILE=/etc/forex-scalper.env
OLD_ENV=/opt/forex-bot/.env
SVC_USER=botuser
PY=python3.13

echo "[1/8] Stopping the old forex-bot (one bot per account/token)..."
sudo systemctl disable --now forex-bot 2>/dev/null && echo "  old forex-bot stopped." \
  || echo "  forex-bot not found / already stopped (continuing)."

echo "[2/8] Ensuring Python 3.13 is installed (this VM ships 3.10)..."
if ! command -v "$PY" >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y software-properties-common
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update -qq
  sudo apt-get install -y python3.13 python3.13-venv
fi
"$PY" --version

echo "[3/8] Reusing the demo creds already on this VM..."
if [ ! -f "$ENVFILE" ]; then
  if [ -f "$OLD_ENV" ]; then
    sudo cp "$OLD_ENV" "$ENVFILE"; echo "  copied creds from $OLD_ENV"
  else
    echo "  ERROR: $ENVFILE missing and $OLD_ENV not found." >&2; exit 1
  fi
fi
sudo chown root:root "$ENVFILE"; sudo chmod 600 "$ENVFILE"

echo "[4/8] Creating the dedicated locked-down service account ($SVC_USER)..."
id "$SVC_USER" >/dev/null 2>&1 \
  || sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"

echo "[5/8] Building the venv + installing (source stays root-owned, read-only)..."
sudo "$PY" -m venv "$APP/.venv"
sudo "$APP/.venv/bin/pip" install --upgrade pip -q
sudo "$APP/.venv/bin/pip" install -q "$APP"
sudo chown -R root:root "$APP"
sudo chmod -R go-w "$APP"

echo "[6/8] Creating the writable runtime dir (owned by $SVC_USER)..."
sudo mkdir -p "$STATE"
sudo chown -R "$SVC_USER:$SVC_USER" "$STATE"

echo "[7/8] Writing the hardened systemd service..."
sudo tee /etc/systemd/system/forex-scalper.service >/dev/null <<UNIT
[Unit]
Description=forex-scalper live bot (DEMO / practice)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$STATE
EnvironmentFile=$ENVFILE
ExecStart=$APP/.venv/bin/python -m forex_scalper.live --practice
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$STATE

[Install]
WantedBy=multi-user.target
UNIT

echo "[8/8] Starting forex-scalper..."
sudo systemctl daemon-reload
sudo systemctl enable forex-scalper
# `restart` (not `enable --now`) so a RE-deploy actually reloads new code:
# on an already-running service `enable --now` is a no-op and the old code
# keeps running in memory.
sudo systemctl restart forex-scalper
sleep 4
sudo systemctl --no-pager status forex-scalper | head -n 18
echo
echo "============================================================"
echo "Done. New bot running on the DEMO account as '$SVC_USER'."
echo "  Live logs:   journalctl -u forex-scalper -f"
echo "  Telegram:    /status   /positions   (alerts on every fill)"
echo "  Trades fire only 12:00-16:00 UTC (London/NY overlap)."
echo "  Roll back:   sudo systemctl disable --now forex-scalper && sudo systemctl enable --now forex-bot"
echo "============================================================"
