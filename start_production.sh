#!/usr/bin/env bash
# ─── Qurachi Bot: Production Start (Caddy + direct domain) ──────────────────
#
# For use with: qurachi.mooo.com (A record → your IP, Caddy handles HTTPS)
#
# Prerequisites:
#   - Caddy installed and configured (/etc/caddy/Caddyfile)
#   - Port 80 + 443 forwarded on your router
#   - PostgreSQL running
#   - .env configured with WEBHOOK_URL=https://qurachi.mooo.com
#
# Usage:
#   ./start_production.sh         # Start bot + web server
#   ./start_production.sh stop    # Stop both
#
# Features:
#   - Auto-update: every night at ~3:00 AM (with 0–30 min random jitter)
#     the bot stops, pulls latest code from git, and restarts automatically.
#
# ─────────────────────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE=".prod_pids"
UPDATER_PID_FILE=".updater_pid"
LOG_FILE="auto_update.log"
GIT_BRANCH="feature/forced-sub-referral-antiabuse"

# ─── Stop mode ─────────────────────────────────────────────
if [ "$1" = "stop" ]; then
    echo "🛑 Stopping Qurachi..."
    # Stop updater
    if [ -f "$UPDATER_PID_FILE" ]; then
        kill "$(cat "$UPDATER_PID_FILE")" 2>/dev/null && echo "  Stopped auto-updater"
        rm -f "$UPDATER_PID_FILE"
    fi
    # Stop bot + web
    if [ -f "$PID_FILE" ]; then
        while read -r pid; do
            kill "$pid" 2>/dev/null && echo "  Stopped PID $pid"
        done < "$PID_FILE"
        rm -f "$PID_FILE"
    fi
    echo "✅ Stopped."
    exit 0
fi

# ─── Load .env ─────────────────────────────────────────────
if [ -f .env ]; then
    export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

# ─── Check Caddy ──────────────────────────────────────────
if ! systemctl is-active --quiet caddy; then
    echo "⚠️  Caddy is not running. Starting it..."
    sudo systemctl start caddy
fi

# ─── Helper: start bot + web ──────────────────────────────
start_services() {
    echo "🌍 Starting web server (port 8080)..."
    python3 web_server.py &
    WEB_PID=$!
    echo "$WEB_PID" > "$PID_FILE"
    sleep 1

    echo "🤖 Starting bot (webhook on port 8443)..."
    python3 main.py &
    BOT_PID=$!
    echo "$BOT_PID" >> "$PID_FILE"
}

# ─── Helper: stop bot + web ──────────────────────────────
stop_services() {
    if [ -f "$PID_FILE" ]; then
        while read -r pid; do
            kill "$pid" 2>/dev/null
        done < "$PID_FILE"
        rm -f "$PID_FILE"
    fi
    # Give processes time to exit
    sleep 2
}

# ─── Auto-updater background process ─────────────────────
auto_updater() {
    while true; do
        # Calculate seconds until next 3:00 AM
        now=$(date +%s)
        # Next 3 AM today or tomorrow
        target_today=$(date -d "today 03:00" +%s 2>/dev/null || date -d "03:00" +%s 2>/dev/null)
        if [ "$target_today" -le "$now" ]; then
            # 3 AM already passed today, aim for tomorrow
            target=$(( target_today + 86400 ))
        else
            target=$target_today
        fi

        # Add random jitter: 0 to 1800 seconds (0–30 minutes)
        jitter=$(( RANDOM % 1800 ))
        sleep_seconds=$(( target - now + jitter ))

        echo "[$(date)] Auto-updater: next update in ${sleep_seconds}s (~$(( sleep_seconds / 3600 ))h $(( (sleep_seconds % 3600) / 60 ))m)" >> "$LOG_FILE"
        sleep "$sleep_seconds"

        echo "[$(date)] Auto-updater: starting update..." >> "$LOG_FILE"

        # Pull latest code
        cd "$SCRIPT_DIR"
        git_output=$(git pull origin "$GIT_BRANCH" 2>&1)
        echo "[$(date)] git pull: $git_output" >> "$LOG_FILE"

        if echo "$git_output" | grep -q "Already up to date"; then
            echo "[$(date)] No changes, skipping restart." >> "$LOG_FILE"
            continue
        fi

        # Stop services
        echo "[$(date)] Stopping services for restart..." >> "$LOG_FILE"
        stop_services

        # Restart services
        echo "[$(date)] Restarting services..." >> "$LOG_FILE"
        start_services
        echo "[$(date)] ✅ Auto-update complete. Bot restarted." >> "$LOG_FILE"
    done
}

# ─── Start services ───────────────────────────────────────
start_services

# ─── Start auto-updater in background ────────────────────
auto_updater &
UPDATER_PID=$!
echo "$UPDATER_PID" > "$UPDATER_PID_FILE"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  🎲 Qurachi Bot is running!"
echo ""
echo "  🌐 Domain:   https://qurachi.mooo.com"
echo "  🤖 Webhook:  https://qurachi.mooo.com/telegram"
echo "  📱 Mini App: https://qurachi.mooo.com/miniapp"
echo ""
echo "  Caddy handles HTTPS automatically (Let's Encrypt)"
echo ""
echo "  🔄 Auto-update: daily at ~3:00 AM (±30min jitter)"
echo "     Logs: $LOG_FILE"
echo ""
echo "  Press Ctrl+C to stop bot + web server + updater"
echo "  (Caddy keeps running as a system service)"
echo "═══════════════════════════════════════════════════"
echo ""

# ─── Wait & cleanup on exit ───────────────────────────────
cleanup() {
    echo ""
    echo "🛑 Stopping..."
    kill "$UPDATER_PID" 2>/dev/null
    rm -f "$UPDATER_PID_FILE"
    stop_services
    echo "✅ Stopped."
    exit 0
}

trap cleanup INT TERM

wait
