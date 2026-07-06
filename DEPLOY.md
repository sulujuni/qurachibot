# Deployment Guide

## Quick Start (Single Worker + Cloudflare Tunnel)

The simplest production setup: one bot process on your computer with a free Cloudflare Tunnel for HTTPS.

### Prerequisites
- Python 3.10+
- PostgreSQL (local or remote)
- `cloudflared` CLI

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set up PostgreSQL
```bash
sudo -u postgres psql
```
```sql
CREATE USER qurachibot WITH PASSWORD 'your_password';
CREATE DATABASE qurachibot OWNER qurachibot;
\q
```

### 3. Configure `.env`
```bash
cp .env.example .env
# Edit .env with your values
```

Key settings:
```bash
BOT_TOKEN=<from @BotFather>
DATABASE_URL=postgresql+asyncpg://qurachibot:your_password@localhost:5432/qurachibot
USE_WEBHOOK=true
WEBHOOK_URL=<from cloudflared output>
WEBHOOK_PORT=8443
WEBHOOK_SECRET_TOKEN=<generate: python -c "import secrets; print(secrets.token_urlsafe(32))">
MAX_CONCURRENT_UPDATES=256
```

### 4. Start the tunnel (Terminal 1)
```bash
cloudflared tunnel --url http://localhost:8443
```
Copy the `https://....trycloudflare.com` URL into `WEBHOOK_URL` in `.env`.

### 5. Start the bot (Terminal 2)
```bash
python main.py
```

---

## Multi-Worker Setup (For Scale)

Run multiple bot workers behind a load balancer to handle thousands of concurrent users.

### Architecture
```
Telegram → Cloudflare Tunnel → nginx:9000 → Workers (8443, 8444, 8445, ...) → PostgreSQL
```

### 1. Nginx load balancer

Install nginx, then create `/etc/nginx/conf.d/qurachibot.conf`:
```nginx
upstream qurachibot_workers {
    least_conn;
    server 127.0.0.1:8443;
    server 127.0.0.1:8444;
    server 127.0.0.1:8445;
}

server {
    listen 9000;

    location /telegram {
        proxy_pass http://qurachibot_workers;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Reload:
```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 2. Start workers

Use the included helper script:
```bash
./run_workers.sh 3      # starts 3 workers on ports 8443-8445
./run_workers.sh stop   # stops all workers
```

Or manually:
```bash
WEBHOOK_PORT=8443 python main.py &
WEBHOOK_PORT=8444 python main.py &
WEBHOOK_PORT=8445 python main.py &
```

### 3. Point tunnel at nginx
```bash
cloudflared tunnel --url http://localhost:9000
```

### 4. Update `.env`
```bash
WEBHOOK_URL=https://your-tunnel-url.trycloudflare.com
```

---

## Permanent Tunnel (Named Tunnel with Custom Domain)

Quick tunnels (`trycloudflare.com`) change URL on every restart. For a stable URL:

```bash
cloudflared tunnel login                                    # authenticate
cloudflared tunnel create qurachibot                        # create tunnel
cloudflared tunnel route dns qurachibot bot.yourdomain.com  # set DNS
cloudflared tunnel run --url http://localhost:9000 qurachibot
```

Then set `WEBHOOK_URL=https://bot.yourdomain.com` permanently in `.env`.

---

## Database Migrations

On first run, the bot creates all tables via `create_all`. For subsequent schema updates:

```bash
# Apply pending migrations
alembic upgrade head

# Or if the DB was created by create_all and you need to stamp it
alembic stamp head
```

---

## Scaling Guidelines

| Users | Setup | Workers |
|-------|-------|---------|
| < 1,000 | Polling mode, SQLite/PG | 1 |
| 1,000 – 50,000 | Webhook, PostgreSQL | 1-3 |
| 50,000 – 500,000 | Webhook, PostgreSQL, nginx | 3-10 |
| 500,000+ | VPS/cloud, webhook, PG, nginx, Redis cache | 10+ |

### Tuning
- `MAX_CONCURRENT_UPDATES`: default 256, increase for very large bursts
- `DB_POOL_SIZE` + `DB_MAX_OVERFLOW`: total connections = workers x (pool + overflow). Example: 3 workers x 50 = 150 max PG connections. Adjust `max_connections` in `postgresql.conf` accordingly.
- PostgreSQL `max_connections`: at least `(workers * (DB_POOL_SIZE + DB_MAX_OVERFLOW)) + 10`

### Why PostgreSQL is required for scale
- SQLite allows only 1 writer at a time → "database is locked" under load
- PostgreSQL handles hundreds of concurrent writes via MVCC
- Connection pooling distributes load across multiple workers

---

## Docker (Alternative)

```bash
docker-compose up -d
```

Uses the included `docker-compose.yml` which starts PostgreSQL + the bot. For multi-worker, scale the bot service:
```bash
docker-compose up -d --scale bot=3
```

---

## Polling Mode (Development)

For local development without a tunnel:
```bash
USE_WEBHOOK=false python main.py
```
Only one process can poll at a time (Telegram limitation).
