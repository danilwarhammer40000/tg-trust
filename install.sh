#!/bin/bash
set -euo pipefail

echo "=== TrustPanel Installer ==="

# -------------------------
# CONFIG
# -------------------------
DEFAULT_DIR="/opt/trustpanel"
PROJECT_DIR="${PROJECT_DIR:-$DEFAULT_DIR}"
# Repository to install from. Override with your own fork:
#   REPO_URL=https://github.com/<you>/tg-trust.git bash install.sh
REPO_URL="${REPO_URL:-https://github.com/danilwarhammer40000/tg-trust.git}"

# -------------------------
# SYSTEM DEPENDENCIES
# -------------------------
echo "[1/9] Installing system dependencies..."

export DEBIAN_FRONTEND=noninteractive
apt update -y
apt install -y python3 python3-venv python3-pip git curl ca-certificates

# -------------------------
# INSTALL / UPDATE
# -------------------------
echo "[2/9] Preparing install directory..."

if [ -d "$PROJECT_DIR/.git" ]; then
    echo "[INFO] Updating repository..."
    cd "$PROJECT_DIR"
    git fetch --all
    # Never discard local changes silently: fast-forward only, otherwise stop.
    if ! git pull --ff-only; then
        echo "[ERROR] Cannot fast-forward (local changes or diverged history)."
        echo "        Inspect: git -C $PROJECT_DIR status"
        exit 1
    fi
else
    echo "[INFO] Cloning repository from $REPO_URL ..."
    if [ -e "$PROJECT_DIR" ] && [ -n "$(ls -A "$PROJECT_DIR" 2>/dev/null)" ]; then
        echo "[ERROR] $PROJECT_DIR exists and is not empty (and is not a git checkout) — refusing to delete it."
        exit 1
    fi
    git clone "$REPO_URL" "$PROJECT_DIR"
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

# Optional — Cloudflare Worker (HTTPS reverse-proxy) for outbound Gemini API
# calls. Google's Gemini API enforces a region allowlist independent of
# API-key validity; a Russian-hosted VPS IP typically gets
# FAILED_PRECONDITION ("User location is not supported") even with a valid
# key. The URL given here is only the default — it can be changed later in
# the bot ("🤖 Автопродление" → "⚙️ Правила проверки" → "🌐 Прокси-адрес")
# without touching .env or restarting anything. Leave blank for a direct
# connection.
echo ""
echo "Cloudflare Worker для Gemini API (опционально, можно поменять позже в боте) —"
echo "адрес вида https://your-worker.workers.dev"
read -r -p "GEMINI_PROXY_URL — Worker URL (Enter чтобы пропустить): " GEMINI_PROXY_URL
GEMINI_PROXY_URL=$(echo "$GEMINI_PROXY_URL" | tr -d '\r')
if [ -n "$GEMINI_PROXY_URL" ] && [[ ! "$GEMINI_PROXY_URL" =~ ^https?:// ]]; then
    echo "[ERROR] GEMINI_PROXY_URL must start with http:// or https:// (Cloudflare Worker URL)"
    exit 1
fi

# -------------------------
# ENV
# -------------------------
echo "[5/9] Writing .env..."

cat > "$PROJECT_DIR/.env" <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_ID=$ADMIN_ID
TRUSTTUNNEL_DOMAIN=$DOMAIN
GEMINI_API_KEY=$GEMINI_API_KEY
GEMINI_PROXY_URL=$GEMINI_PROXY_URL
LOG_CHANNEL_ID=$LOG_CHANNEL_ID
PYTHONPATH=$PROJECT_DIR
EOF

chmod 600 "$PROJECT_DIR/.env"

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

echo ""
echo "=== TIMERS ==="
systemctl list-timers | grep trustpanel || true

echo ""
echo "DONE"
echo "Bot logs: journalctl -u trustpanel-bot.service -f"
