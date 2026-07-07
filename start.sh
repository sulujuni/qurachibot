#!/usr/bin/env bash
# ─── Qurachi Bot: Auto-Start with Cloudflare Tunnel ─────────────────────────
#
# This script:
# 1. Starts a Cloudflare quick tunnel (free, no domain needed)
# 2. Captures the generated HTTPS URL
# 3. Exports it as WEBHOOK_URL and WEB_URL
# 4. Starts both the bot and web server
#
# Usage:
#   ./start.sh          # Start everything
#   ./start.sh stop     # Stop everything
#
# Requirements: cloudflared, python3, .env file with BOT_TOKEN + DATABASE_URL
# ─────────────────────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

PID_FILE=".pids"

# ─── Stop mode ────────────────────────────────────────────────────────────────
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

# ─── Check requirements ──────────────────────────────────────────────────────
command -v cloudflared >/dev/null 2>&1 || { echo "❌ cloudflared is not installed. Install it first."; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "❌ python3 not found."; exit 1; }

echo "🎲 Starting Qurachi Bot..."
echo ""

# ─── Start Cloudflare Tunnel ──────────────────────────────────────────────────
echo "🌐 Starting Cloudflare Tunnel..."

# Start tunnel in background, capture the URL from its output
TUNNEL_LOG=$(mktemp)
cloudflared tunnel --url http://localhost:9000 --no-autoupdate > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!
echo "$TUNNEL_PID" > "$PID_FILE"

# Wait for the tunnel URL to appear (up to 15 seconds)
TUNNEL_URL=""
for i in $(seq 1 30); do
    sleep 0.5
    TUNNEL_URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1)
    if [ -n "$TUNNEL_URL" ]; then
        break
    fi
done
rm -f "$TUNNEL_LOG"

if [ -z "$TUNNEL_URL" ]; then
    echo "❌ Failed to get tunnel URL. Check your internet connection."
    kill "$TUNNEL_PID" 2>/dev/null
    rm -f "$PID_FILE"
    exit 1
fi

echo "✅ Tunnel ready: $TUNNEL_URL"
echo ""

# ─── Export URLs ──────────────────────────────────────────────────────────────
export WEBHOOK_URL="$TUNNEL_URL"
export WEB_URL="$TUNNEL_URL"
export USE_WEBHOOK=true
export WEBHOOK_PORT=8443

# ─── Start nginx (simple reverse proxy) ──────────────────────────────────────
# Instead of nginx, we'll use a Python-based simple proxy approach:
# The tunnel points to port 9000, we route:
#   /telegram → bot webhook (port 8443)
#   everything else → web server (port 8080)
echo "🔀 Starting reverse proxy (port 9000)..."
python3 -c "
import asyncio
from aiohttp import web, ClientSession

async def proxy_handler(request):
    path = request.path
    if path.startswith('/telegram'):
        target = 'http://127.0.0.1:8443'
    else:
        target = 'http://127.0.0.1:8080'

    target_url = f'{target}{path}'
    async with ClientSession() as session:
        try:
            async with session.request(
                request.method, target_url,
                headers={k: v for k, v in request.headers.items() if k.lower() != 'host'},
                data=await request.read(),
            ) as resp:
                body = await resp.read()
                return web.Response(body=body, status=resp.status,
                    headers={k: v for k, v in resp.headers.items() if k.lower() not in ('transfer-encoding',)})
        except Exception as e:
            return web.Response(text=str(e), status=502)

app = web.Application()
app.router.add_route('*', '/{path_info:.*}', proxy_handler)
web.run_app(app, host='127.0.0.1', port=9000, print=None)
" &
PROXY_PID=$!
echo "$PROXY_PID" >> "$PID_FILE"
sleep 1

# ─── Start web server ─────────────────────────────────────────────────────────
echo "🌍 Starting web server (port 8080)..."
python3 web_server.py &
WEB_PID=$!
echo "$WEB_PID" >> "$PID_FILE"
sleep 1

# ─── Start bot ────────────────────────────────────────────────────────────────
echo "🤖 Starting bot (webhook on port 8443)..."
echo ""
echo "═══════════════════════════════════════════════════"
echo "  🎲 Qurachi Bot is running!"
echo ""
echo "  🌐 Public URL: $TUNNEL_URL"
echo "  🤖 Webhook:    $TUNNEL_URL/telegram"
echo "  📱 Mini App:   $TUNNEL_URL/miniapp"
echo ""
echo "  Press Ctrl+C to stop everything"
echo "═══════════════════════════════════════════════════"
echo ""

python3 main.py &
BOT_PID=$!
echo "$BOT_PID" >> "$PID_FILE"

# ─── Wait and cleanup on exit ─────────────────────────────────────────────────
trap "echo ''; echo '🛑 Stopping...'; kill $TUNNEL_PID $PROXY_PID $WEB_PID $BOT_PID 2>/dev/null; rm -f $PID_FILE; echo '✅ Stopped.'; exit 0" INT TERM

wait
