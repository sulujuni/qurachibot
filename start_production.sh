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
#   - Auto-update: reads schedule from update_config.json
#   - Restart time configurable via Mini App admin panel
#   - Random jitter to avoid exact restart times
#
# ─────────────────────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE=".prod_pids"
UPDATER_PID_FILE=".updater_pid"
LOG_FILE="auto_update.log"
CONFIG_FILE="update_config.json"
GIT_BRANCH="feature/forced-sub-referral-antiabuse"

# ─── Stop mode ─────────────────────────────────────────────
if [ "$1" = "stop" ]; then
    echo "🛑 Stopping Qurachi..."
    # Stop updater + trigger watcher
    if [ -f "$UPDATER_PID_FILE" ]; then
        while read -r pid; do
            kill "$pid" 2>/dev/null && echo "  Stopped background PID $pid"
        done < "$UPDATER_PID_FILE"
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

# ─── Run database migrations ──────────────────────────────
if [ -d "migrations" ]; then
    echo "🗄  Running database migrations..."
    for sql_file in migrations/*.sql; do
        [ -f "$sql_file" ] || continue
        echo "   → $sql_file"
        psql "$DATABASE_URL" -f "$sql_file" 2>/dev/null || \
        psql -f "$sql_file" 2>/dev/null || \
        echo "   ⚠️  Could not run $sql_file (check DB connection)"
    done
    echo "   Done."
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

# ─── Read config from update_config.json ──────────────────
read_config() {
    if [ -f "$CONFIG_FILE" ]; then
        CFG_HOUR=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE')).get('restart_hour',3))" 2>/dev/null || echo 3)
        CFG_MIN=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE')).get('restart_minute',0))" 2>/dev/null || echo 0)
        CFG_JITTER=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE')).get('jitter_minutes',30))" 2>/dev/null || echo 30)
        CFG_ENABLED=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE')).get('enabled',True))" 2>/dev/null || echo "True")
    else
        CFG_HOUR=3
        CFG_MIN=0
        CFG_JITTER=30
        CFG_ENABLED="True"
    fi
}

# ─── Auto-updater background process ─────────────────────
auto_updater() {
    while true; do
        # Re-read config each cycle so Mini App changes take effect immediately
        read_config

        # If disabled, sleep 5 min then re-check
        if [ "$CFG_ENABLED" = "False" ] || [ "$CFG_ENABLED" = "false" ]; then
            echo "[$(date)] Auto-updater: disabled in config. Rechecking in 5min." >> "$LOG_FILE"
            sleep 300
            continue
        fi

        # Calculate seconds until target time
        now=$(date +%s)
        target_today=$(date -d "today ${CFG_HOUR}:$(printf '%02d' $CFG_MIN)" +%s 2>/dev/null || date -d "${CFG_HOUR}:$(printf '%02d' $CFG_MIN)" +%s 2>/dev/null)
        if [ "$target_today" -le "$now" ]; then
            # Target time already passed today, aim for tomorrow
            target=$(( target_today + 86400 ))
        else
            target=$target_today
        fi

        # Add random jitter: 0 to (CFG_JITTER * 60) seconds
        jitter_seconds=$(( CFG_JITTER * 60 ))
        if [ "$jitter_seconds" -gt 0 ]; then
            jitter=$(( RANDOM % jitter_seconds ))
        else
            jitter=0
        fi
        sleep_seconds=$(( target - now + jitter ))

        echo "[$(date)] Auto-updater: scheduled for ${CFG_HOUR}:$(printf '%02d' $CFG_MIN) +${jitter}s jitter (sleeping ${sleep_seconds}s)" >> "$LOG_FILE"
        sleep "$sleep_seconds"

        # Re-read config after waking up (admin may have disabled while we slept)
        read_config
        if [ "$CFG_ENABLED" = "False" ] || [ "$CFG_ENABLED" = "false" ]; then
            echo "[$(date)] Auto-updater: disabled after wakeup, skipping." >> "$LOG_FILE"
            continue
        fi

        echo "[$(date)] Auto-updater: starting update..." >> "$LOG_FILE"

        # Pull latest code
        cd "$SCRIPT_DIR"
        git_output=$(git pull origin "$GIT_BRANCH" 2>&1)
        echo "[$(date)] git pull: $git_output" >> "$LOG_FILE"

        if echo "$git_output" | grep -q "Already up to date"; then
            echo "[$(date)] No changes, skipping restart." >> "$LOG_FILE"
            continue
        fi

        # Run migrations if any new .sql files
        if [ -d "migrations" ]; then
            for sql_file in migrations/*.sql; do
                [ -f "$sql_file" ] || continue
                psql "$DATABASE_URL" -f "$sql_file" >> "$LOG_FILE" 2>&1 || true
            done
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

# ─── Manual restart trigger watcher ───────────────────────
# Checks every 10 seconds for .restart_trigger file (written by Mini App admin)
trigger_watcher() {
    while true; do
        sleep 10
        if [ -f ".restart_trigger" ]; then
            echo "[$(date)] Trigger watcher: manual restart requested!" >> "$LOG_FILE"
            rm -f ".restart_trigger"

            # Pull latest code
            cd "$SCRIPT_DIR"
            git_output=$(git pull origin "$GIT_BRANCH" 2>&1)
            echo "[$(date)] git pull: $git_output" >> "$LOG_FILE"

            # Run migrations
            if [ -d "migrations" ]; then
                for sql_file in migrations/*.sql; do
                    [ -f "$sql_file" ] || continue
                    psql "$DATABASE_URL" -f "$sql_file" >> "$LOG_FILE" 2>&1 || true
                done
            fi

            # Restart services (even if no changes — admin wants a restart)
            echo "[$(date)] Stopping services for manual restart..." >> "$LOG_FILE"
            stop_services

            echo "[$(date)] Restarting services..." >> "$LOG_FILE"
            start_services
            echo "[$(date)] ✅ Manual restart complete." >> "$LOG_FILE"
        fi
    done
}

# ─── Start services ───────────────────────────────────────
start_services

# ─── Start auto-updater in background ────────────────────
auto_updater &
UPDATER_PID=$!
echo "$UPDATER_PID" > "$UPDATER_PID_FILE"

# ─── Start trigger watcher in background ─────────────────
trigger_watcher &
TRIGGER_PID=$!
echo "$TRIGGER_PID" >> "$UPDATER_PID_FILE"

# Read config for display
read_config

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
echo "  🔄 Auto-update: daily at ${CFG_HOUR}:$(printf '%02d' $CFG_MIN) (±${CFG_JITTER}min jitter)"
echo "     Enabled: $CFG_ENABLED"
echo "     Config:  $CONFIG_FILE"
echo "     Logs:    $LOG_FILE"
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
    kill "$TRIGGER_PID" 2>/dev/null
    rm -f "$UPDATER_PID_FILE"
    stop_services
    echo "✅ Stopped."
    exit 0
}

trap cleanup INT TERM

wait
