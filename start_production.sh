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
# Branch the auto-updater pulls from. Defaults to whatever branch is currently
# checked out (so it never goes stale after a branch switch); override by
# exporting GIT_BRANCH before launching.
GIT_BRANCH="${GIT_BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}"

# ─── Stop mode ─────────────────────────────────────────────
if [ "$1" = "stop" ]; then
    echo "🛑 Stopping Qurachi..."
    # Stop cloudflared
    pkill -f "cloudflared tunnel" 2>/dev/null && echo "  Stopped cloudflared"
    # Stop updater + trigger watcher
    if [ -f "$UPDATER_PID_FILE" ]; then
        while read -r pid; do
            kill "$pid" 2>/dev/null && echo "  Stopped background PID $pid"
        done < "$UPDATER_PID_FILE"
        rm -f "$UPDATER_PID_FILE"
    fi
    # Stop bot + web + cloudflared
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

# ─── Database migrations are handled by Python on bot startup (main.py) ───
# No need for shell-based psql here — the bot runs ALTER TABLE IF NOT EXISTS
# via SQLAlchemy when it starts.

# ─── Helper: start bot + web ──────────────────────────────
start_services() {
    echo "🌍 Starting web server (port 8090)..."
    WEB_PORT=8090 python3 web_server.py &
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

            # Restart services (even if no changes — admin wants a restart)
            echo "[$(date)] Stopping services for manual restart..." >> "$LOG_FILE"
            stop_services

            echo "[$(date)] Restarting services..." >> "$LOG_FILE"
            start_services
            echo "[$(date)] ✅ Manual restart complete." >> "$LOG_FILE"
        fi
    done
}

# ─── Start Cloudflare tunnel (port 8090) ──────────────────
echo "☁️  Starting Cloudflare tunnel (localhost:8090)..."
cloudflared tunnel --url http://localhost:8090 > cloudflared.log 2>&1 &
CLOUDFLARED_PID=$!
echo "$CLOUDFLARED_PID" >> "$PID_FILE"

# Wait for the tunnel URL to appear in the log (up to 15 seconds)
echo "⏳ Waiting for tunnel URL..."
TUNNEL_URL=""
for i in $(seq 1 30); do
    sleep 0.5
    TUNNEL_URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' cloudflared.log 2>/dev/null | head -1)
    if [ -n "$TUNNEL_URL" ]; then
        break
    fi
done

if [ -n "$TUNNEL_URL" ]; then
    echo "✅ Tunnel URL: $TUNNEL_URL"

    # Auto-update WEB_URL in .env
    if [ -f .env ]; then
        if grep -q "^WEB_URL=" .env; then
            sed -i "s|^WEB_URL=.*|WEB_URL=$TUNNEL_URL|" .env
        else
            echo "WEB_URL=$TUNNEL_URL" >> .env
        fi
        echo "📝 Updated WEB_URL in .env"
    fi

    # Export for the bot process
    export WEB_URL="$TUNNEL_URL"
else
    echo "⚠️  Could not detect tunnel URL (check cloudflared.log)"
    echo "    Bot will use existing WEB_URL from .env"
fi

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
if [ -n "$TUNNEL_URL" ]; then
echo "  ☁️  Tunnel:   $TUNNEL_URL"
echo "  📱 Mini App: $TUNNEL_URL/miniapp"
else
echo "  🌐 Domain:   https://qurachi.mooo.com"
echo "  📱 Mini App: https://qurachi.mooo.com/miniapp"
fi
echo ""
echo "  🔄 Auto-update: daily at ${CFG_HOUR}:$(printf '%02d' $CFG_MIN) (±${CFG_JITTER}min jitter)"
echo "     Enabled: $CFG_ENABLED"
echo "     Config:  $CONFIG_FILE"
echo "     Logs:    $LOG_FILE"
echo ""
echo "  Press Ctrl+C to stop bot + web server + updater"
echo "═══════════════════════════════════════════════════"
echo ""

# ─── Wait & cleanup on exit ───────────────────────────────
cleanup() {
    echo ""
    echo "🛑 Stopping..."
    kill "$UPDATER_PID" 2>/dev/null
    kill "$TRIGGER_PID" 2>/dev/null
    kill "$CLOUDFLARED_PID" 2>/dev/null
    rm -f "$UPDATER_PID_FILE"
    stop_services
    echo "✅ Stopped."
    exit 0
}

trap cleanup INT TERM

wait
