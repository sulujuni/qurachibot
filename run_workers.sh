#!/usr/bin/env bash
# ─── Qurachi Bot: Multi-Worker Launcher ──────────────────────────────────────
#
# Starts N webhook workers on consecutive ports behind a load balancer.
# Each worker is an independent process sharing the same PostgreSQL database.
#
# Prerequisites:
#   1. PostgreSQL running with DATABASE_URL configured in .env
#   2. nginx configured to reverse-proxy to the worker ports (see DEPLOY.md)
#   3. cloudflared tunnel pointing at the nginx port
#
# Usage:
#   ./run_workers.sh          # starts 3 workers (default)
#   ./run_workers.sh 5        # starts 5 workers
#   ./run_workers.sh stop     # stops all workers
#
# ──────────────────────────────────────────────────────────────────────────────

set -e

NUM_WORKERS=${1:-3}
BASE_PORT=${BASE_PORT:-8443}
PID_DIR=".worker_pids"

# ─── Stop mode ────────────────────────────────────────────────────────────────
if [ "$1" = "stop" ]; then
    echo "Stopping all workers..."
    if [ -d "$PID_DIR" ]; then
        for pidfile in "$PID_DIR"/*.pid; do
            [ -f "$pidfile" ] || continue
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                echo "  Stopping worker PID $pid"
                kill "$pid"
            fi
            rm -f "$pidfile"
        done
        rmdir "$PID_DIR" 2>/dev/null || true
    fi
    echo "All workers stopped."
    exit 0
fi

# ─── Start mode ───────────────────────────────────────────────────────────────
echo "Starting $NUM_WORKERS workers (ports $BASE_PORT–$((BASE_PORT + NUM_WORKERS - 1)))..."
mkdir -p "$PID_DIR"

for i in $(seq 0 $((NUM_WORKERS - 1))); do
    port=$((BASE_PORT + i))
    echo "  Worker $((i + 1)): port $port"
    WEBHOOK_PORT=$port python main.py &
    echo $! > "$PID_DIR/worker_${port}.pid"
done

echo ""
echo "All $NUM_WORKERS workers started."
echo ""
echo "Next steps:"
echo "  1. Ensure nginx is configured to load-balance across ports $BASE_PORT–$((BASE_PORT + NUM_WORKERS - 1))"
echo "  2. Start your Cloudflare tunnel: cloudflared tunnel --url http://localhost:9000"
echo ""
echo "To stop all workers: ./run_workers.sh stop"

# Wait for all background jobs (Ctrl+C kills them all)
wait
