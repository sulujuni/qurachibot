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
# ─────────────────────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE=".prod_pids"

# ─── Stop mode ─────────────────────────────────────────────
if [ "$1" = "stop" ]; then
    echo "🛑 Stopping Qurachi..."
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

# ─── Start web server ────────────────────────────────────────
echo "🌍 Starting web server (port 8080)..."
python3 web_server.py &
WEB_PID=$!
echo "$WEB_PID" > "$PID_FILE"
sleep 1

# ─── Start bot ────────────────────────────────────────────────
echo "🤖 Starting bot (webhook on port 8443)..."
python3 main.py &
BOT_PID=$!
echo "$BOT_PID" >> "$PID_FILE"

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
echo "  Press Ctrl+C to stop bot + web server"
echo "  (Caddy keeps running as a system service)"
echo "═══════════════════════════════════════════════════"
echo ""

# ─── Wait ─────────────────────────────────────────────────────
trap "echo ''; echo '🛑 Stopping...'; kill $WEB_PID $BOT_PID 2>/dev/null; rm -f $PID_FILE; echo '✅ Stopped.'; exit 0" INT TERM

wait
