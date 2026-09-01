#!/bin/bash
set -euo pipefail

echo "=== TrustPanel Installer ==="

# -------------------------
# CONFIG
# -------------------------
DEFAULT_DIR="/opt/trustpanel"
PROJECT_DIR="${PROJECT_DIR:-$DEFAULT_DIR}"

# -------------------------
# SYSTEM DEPENDENCIES
# -------------------------
echo "[1/9] Installing system dependencies..."

export DEBIAN_FRONTEND=noninteractive
apt update -y
apt upgrade -y
apt install -y python3 python3-venv python3-pip git curl ca-certificates

# -------------------------
# INSTALL / UPDATE
# -------------------------
echo "[2/9] Preparing install directory..."

if [ -d "$PROJECT_DIR/.git" ]; then
    echo "[INFO] Updating repository..."
    cd "$PROJECT_DIR"
    git fetch --all
    git reset --hard origin/main
    git pull --ff-only
else
    echo "[INFO] Cloning repository..."
    rm -rf "$PROJECT_DIR"
    git clone https://github.com/danilwarhammer40000/tg-trust.git "$PROJECT_DIR"
    cd "$PROJECT_DIR"
fi

# -------------------------
# VENV
# -------------------------
echo "[3/9] Setting up virtual environment..."

if [ ! -d "$PROJECT_DIR/venv" ]; then
    python3 -m venv "$PROJECT_DIR/venv"
fi

source "$PROJECT_DIR/venv/bin/activate"

python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# -------------------------
# INPUT
# -------------------------
echo "[4/9] Configuration"

read -r -p "BOT_TOKEN: " BOT_TOKEN
read -r -p "ADMIN_ID: " ADMIN_ID
read -r -p "TRUSTTUNNEL_DOMAIN: " DOMAIN

if [[ -z "$BOT_TOKEN" || -z "$ADMIN_ID" || -z "$DOMAIN" ]]; then
    echo "[ERROR] Required variables missing"
    exit 1
fi

BOT_TOKEN=$(echo "$BOT_TOKEN" | tr -d '\r')
ADMIN_ID=$(echo "$ADMIN_ID" | tr -d '\r')
DOMAIN=$(echo "$DOMAIN" | tr -d '\r')

# Optional — the MAX bot (max_bot/) only gets installed/started if this is
# given. Leave blank and press Enter to skip it entirely.
read -r -p "MAX_BOT_TOKEN (Enter to skip MAX bot): " MAX_BOT_TOKEN
MAX_BOT_TOKEN=$(echo "$MAX_BOT_TOKEN" | tr -d '\r')

# Optional — AI auto-renewal (🤖 Автопродление). Both empty = feature
# stays off and unconfigured; the bot itself also refuses to let it be
# turned ON without LOG_CHANNEL_ID (every decision must be logged).
echo ""
echo "AI auto-renewal (optional) — reads payment receipts via Gemini and can"
echo "auto-extend access at night / when a request sits unanswered too long."
echo "Get a free key: https://aistudio.google.com/apikey"
read -r -p "GEMINI_API_KEY (Enter to skip this feature): " GEMINI_API_KEY
GEMINI_API_KEY=$(echo "$GEMINI_API_KEY" | tr -d '\r')

read -r -p "LOG_CHANNEL_ID (numeric channel id, needed for auto-renewal, Enter to skip): " LOG_CHANNEL_ID
LOG_CHANNEL_ID=$(echo "$LOG_CHANNEL_ID" | tr -d '\r')

# Optional — only relevant if GEMINI_API_KEY was given above. Google's
# Generative Language API rejects requests from some server locations
# with HTTP 400 "User location is not supported for the API use" —
# unrelated to the API key itself, just where the request physically
# comes from. If that happens (see core/gemini_client.py's error message
# for the exact symptom), route ONLY the Gemini call through a SOCKS5/HTTP
# proxy sitting in a supported region (any EU country works) — deliberately
# NOT the generic HTTP_PROXY/HTTPS_PROXY env vars, since those would also
# silently redirect Telegram/MAX Bot API traffic through the same proxy.
if [ -n "$GEMINI_API_KEY" ]; then
    echo ""
    echo "If Gemini rejects requests from this server's location (HTTP 400"
    echo "'User location is not supported'), set a proxy here — e.g. a"
    echo "SOCKS5 proxy on a cheap VM in any supported country (EU works)."
    echo "Format: socks5h://user:pass@host:port  or  http://user:pass@host:port"
    read -r -p "GEMINI_PROXY_URL (Enter to skip, use direct connection): " GEMINI_PROXY_URL
    GEMINI_PROXY_URL=$(echo "$GEMINI_PROXY_URL" | tr -d '\r')
else
    GEMINI_PROXY_URL=""
fi

# -------------------------
# ENV
# -------------------------
echo "[5/9] Writing .env..."

cat > "$PROJECT_DIR/.env" <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_ID=$ADMIN_ID
TRUSTTUNNEL_DOMAIN=$DOMAIN
MAX_BOT_TOKEN=$MAX_BOT_TOKEN
GEMINI_API_KEY=$GEMINI_API_KEY
LOG_CHANNEL_ID=$LOG_CHANNEL_ID
GEMINI_PROXY_URL=$GEMINI_PROXY_URL
PYTHONPATH=$PROJECT_DIR
EOF

chmod 600 "$PROJECT_DIR/.env"

if [ -n "$MAX_BOT_TOKEN" ]; then
    echo "[INFO] MAX_BOT_TOKEN given — installing maxapi..."
    pip install -r requirements-max.txt
fi

# -------------------------
# DATA DIRECTORY (users.json etc. contain plaintext VPN passwords)
# -------------------------
echo "[6/9] Preparing data directory..."

mkdir -p "$PROJECT_DIR/data"
chmod 700 "$PROJECT_DIR/data"

# -------------------------
# SYSTEMD UNITS
# -------------------------
echo "[7/9] Installing systemd units..."

install_unit () {
    local name=$1
    local src="$PROJECT_DIR/systemd/$name"

    if [ -f "$src" ]; then
        echo "[INFO] Installing $name"
        cp "$src" "/etc/systemd/system/$name"
        systemctl enable "$name"
    else
        echo "[SKIP] $name not found"
    fi
}

# CHANGED: trustpanel-bot.service now comes from the repo, same as the
# cleanup/backup units, instead of a separate heredoc maintained here. This
# was a real source of drift — the old heredoc had different hardening
# settings than the file committed under systemd/, and only the heredoc
# version was ever actually installed.
install_unit "trustpanel-bot.service"
install_unit "trustpanel-cleanup.service"
install_unit "trustpanel-backup.service"
install_unit "trustpanel-post-disable.service"
install_unit "trustpanel-auto-renewal-check.service"
install_unit "trustpanel-cleanup.timer"
install_unit "trustpanel-backup.timer"
install_unit "trustpanel-post-disable.timer"
install_unit "trustpanel-auto-renewal-check.timer"

if [ -n "$MAX_BOT_TOKEN" ]; then
    install_unit "trustpanel-max-bot.service"
fi

# -------------------------
# SYSTEMD APPLY
# -------------------------
echo "[8/9] Reloading systemd..."

systemctl daemon-reload

systemctl stop trustpanel-bot.service 2>/dev/null || true

systemctl enable trustpanel-bot.service
systemctl restart trustpanel-bot.service

systemctl start trustpanel-cleanup.timer 2>/dev/null || true
systemctl start trustpanel-backup.timer 2>/dev/null || true
systemctl start trustpanel-post-disable.timer 2>/dev/null || true
systemctl start trustpanel-auto-renewal-check.timer 2>/dev/null || true

if [ -n "$MAX_BOT_TOKEN" ]; then
    systemctl stop trustpanel-max-bot.service 2>/dev/null || true
    systemctl enable trustpanel-max-bot.service
    systemctl restart trustpanel-max-bot.service
fi

# -------------------------
# HEALTH CHECK
# -------------------------
echo "[9/9] Checking services..."

sleep 3

echo ""
echo "=== BOT STATUS ==="
if systemctl is-active --quiet trustpanel-bot.service; then
    echo "✅ BOT RUNNING"
else
    echo "❌ BOT FAILED"
    systemctl status trustpanel-bot.service --no-pager || true
fi

if [ -n "$MAX_BOT_TOKEN" ]; then
    echo ""
    echo "=== MAX BOT STATUS ==="
    if systemctl is-active --quiet trustpanel-max-bot.service; then
        echo "✅ MAX BOT RUNNING"
    else
        echo "❌ MAX BOT FAILED"
        systemctl status trustpanel-max-bot.service --no-pager || true
    fi
fi

echo ""
echo "=== TIMERS ==="
systemctl list-timers | grep trustpanel || true

echo ""
echo "DONE"
echo "Bot logs: journalctl -u trustpanel-bot.service -f"
