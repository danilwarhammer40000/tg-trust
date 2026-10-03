#!/bin/bash
set -euo pipefail

echo "=== TrustPanel Deploy / Update ==="

# -------------------------
# CONFIG
# -------------------------
DEFAULT_DIR="/opt/trustpanel"
PROJECT_DIR="${PROJECT_DIR:-$DEFAULT_DIR}"

# Mirror everything this script prints into a log file too, so on_error()
# below can attach the last few lines of real context to its Telegram
# notification instead of just "something failed, go check journalctl".
LOG_FILE="$(mktemp)"
exec > >(tee -a "$LOG_FILE") 2>&1

# Best-effort Telegram notification. A silent no-op until .env has been
# sourced (BOT_TOKEN/ADMIN_ID unset) — i.e. the two checks below, before
# cd/source .env, can't notify on failure; everything from "1) PULL LATEST
# CODE" onward can.
notify() {
    if [ -n "${BOT_TOKEN:-}" ] && [ -n "${ADMIN_ID:-}" ]; then
        curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d chat_id="${ADMIN_ID}" \
            --data-urlencode "text=$1" > /dev/null || true
    fi
}

# CHANGED: previously this script only ever notified Telegram at the very
# end (final success, or the step-6 health-check failure) — any failure in
# steps 1-3/5 (git pull rejected, pip install error, permission issue
# copying a unit file, ...) died silently thanks to `set -euo pipefail`:
# the admin saw nothing in the bot at all, only the "🚀 Запускаю деплой..."
# message and then silence, even though the script (and the log it left in
# journalctl) had the real reason the whole time. This trap now fires on
# ANY command that fails under `set -e` (commands intentionally allowed to
# fail already use `|| true` throughout this script, so they don't trigger
# it) and reports where + why immediately.
on_error() {
    local exit_code=$? line_no=$1
    notify "❌ Деплой упал (строка ${line_no}, код ${exit_code}).

Последние строки лога:
$(tail -n 25 "$LOG_FILE")"
}
trap 'on_error $LINENO' ERR

if [ ! -d "$PROJECT_DIR/.git" ]; then
    echo "[ERROR] $PROJECT_DIR is not a git checkout."
    echo "        This looks like a fresh machine — run install.sh instead."
    exit 1
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "[ERROR] $PROJECT_DIR/.env not found."
    echo "        This script never touches .env — run install.sh once first"
    echo "        to generate it, then use deploy.sh for all future updates."
    exit 1
fi

cd "$PROJECT_DIR"

set -a
source .env
set +a

# -------------------------
# 1) PULL LATEST CODE
# -------------------------
echo "[1/6] Pulling latest code..."

git fetch --all

LOCAL=$(git rev-parse @)
REMOTE=$(git rev-parse @{u})

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "[INFO] Already up to date (no code changes)."
else
    if ! git merge --ff-only @{u}; then
        echo "[ERROR] Local branch has diverged from origin/main (local commits or"
        echo "        edits exist on the server). Refusing to overwrite automatically."
        echo "        Inspect with: git -C $PROJECT_DIR status"
        echo "        If you're sure it's safe to discard local changes, run:"
        echo "          git -C $PROJECT_DIR reset --hard origin/main"
        exit 1
    fi
    echo "[OK] Updated: $LOCAL -> $(git rev-parse @)"
fi

# -------------------------
# 2) DEPENDENCIES
# -------------------------
echo "[2/6] Syncing Python dependencies..."

source "$PROJECT_DIR/venv/bin/activate"

pip install -r requirements.txt --quiet

if [ -n "${MAX_BOT_TOKEN:-}" ]; then
    pip install -r requirements-max.txt --quiet
fi

# -------------------------
# 3) REFRESH SYSTEMD UNITS
# -------------------------
echo "[3/6] Refreshing systemd unit files..."

UNITS_CHANGED=0
BOT_UNIT_CHANGED=0

install_unit_if_changed () {
    local name=$1
    local src="$PROJECT_DIR/systemd/$name"
    local dst="/etc/systemd/system/$name"

    if [ ! -f "$src" ]; then
        echo "[SKIP] $name not found in repo"
        return
    fi

    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        echo "[OK] $name unchanged"
        return
    fi

    echo "[UPDATE] $name"
    cp "$src" "$dst"
    systemctl enable "$name" > /dev/null 2>&1 || true
    UNITS_CHANGED=1

    if [ "$name" = "trustpanel-bot.service" ]; then
        BOT_UNIT_CHANGED=1
    fi
}

# trustpanel-bot.service is also managed from the repo, same as the
# cleanup/backup units — previously it was only ever written once by
# install.sh's heredoc, so any later hardening change to the unit file
# never reached already-installed servers without a manual edit.
install_unit_if_changed "trustpanel-bot.service"
install_unit_if_changed "trustpanel-cleanup.service"
install_unit_if_changed "trustpanel-cleanup.timer"
install_unit_if_changed "trustpanel-backup.service"
install_unit_if_changed "trustpanel-backup.timer"
install_unit_if_changed "trustpanel-post-disable.service"
install_unit_if_changed "trustpanel-post-disable.timer"
install_unit_if_changed "trustpanel-auto-renewal-check.service"
install_unit_if_changed "trustpanel-auto-renewal-check.timer"

if [ -n "${MAX_BOT_TOKEN:-}" ]; then
    install_unit_if_changed "trustpanel-max-bot.service"
fi

if [ "$UNITS_CHANGED" = "1" ]; then
    systemctl daemon-reload
fi

if [ "$BOT_UNIT_CHANGED" = "1" ]; then
    echo "[INFO] trustpanel-bot.service definition changed on disk."
fi

# -------------------------
# 4) RESTART BOT
# -------------------------
echo "[4/6] Restarting bot..."

systemctl restart trustpanel-bot.service

if [ -n "${MAX_BOT_TOKEN:-}" ]; then
    systemctl restart trustpanel-max-bot.service
fi

# -------------------------
# 5) RESTART TIMERS (only if their unit actually changed — a timer restart
#    re-schedules the next run, which matters if OnCalendar/OnUnitActiveSec
#    was edited, e.g. hourly -> daily)
# -------------------------
echo "[5/6] Restarting changed timers..."

if [ "$UNITS_CHANGED" = "1" ]; then
    systemctl restart trustpanel-cleanup.timer 2>/dev/null || true
    systemctl restart trustpanel-backup.timer 2>/dev/null || true
    systemctl restart trustpanel-post-disable.timer 2>/dev/null || true
    systemctl restart trustpanel-auto-renewal-check.timer 2>/dev/null || true
else
    echo "[INFO] No unit changes — leaving timers as-is"
fi

# -------------------------
# 6) HEALTH CHECK
# -------------------------
echo "[6/6] Checking services..."

sleep 3

echo ""
echo "=== BOT STATUS ==="
if systemctl is-active --quiet trustpanel-bot.service; then
    echo "✅ BOT RUNNING"
else
    echo "❌ BOT FAILED"
    systemctl status trustpanel-bot.service --no-pager || true
    echo ""
    echo "Recent logs:"
    journalctl -u trustpanel-bot.service -n 40 --no-pager || true

    notify "❌ Деплой прошёл, но бот не запустился. Проверьте journalctl -u trustpanel-bot.service"

    # Disarm the ERR trap before exiting on purpose here -- we already
    # sent the specific, more useful "bot failed to start" message above;
    # letting the trap ALSO fire on this `exit 1` would send a second,
    # redundant "deploy failed at line N" message right after it.
    trap - ERR
    exit 1
fi

echo ""
echo "=== TIMERS ==="
systemctl list-timers | grep trustpanel || true

if [ -n "${MAX_BOT_TOKEN:-}" ]; then
    echo ""
    echo "=== MAX BOT STATUS ==="
    if systemctl is-active --quiet trustpanel-max-bot.service; then
        echo "✅ MAX BOT RUNNING"
    else
        echo "❌ MAX BOT FAILED"
        systemctl status trustpanel-max-bot.service --no-pager || true
        journalctl -u trustpanel-max-bot.service -n 40 --no-pager || true
    fi
fi

notify "✅ Деплой завершён, бот работает."

echo ""
echo "DONE"
echo "Bot logs: journalctl -u trustpanel-bot.service -f"
echo "Cleanup logs: journalctl -u trustpanel-cleanup.service -f"
