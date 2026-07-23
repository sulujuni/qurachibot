"""Bot configuration loaded from environment variables."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:password@localhost:5432/qurachibot"
    )
    ADMIN_IDS: list = field(default_factory=lambda: [
        int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
    ])
    WEB_HOST: str = os.getenv("WEB_HOST", "127.0.0.1")
    WEB_PORT: int = int(os.getenv("WEB_PORT", "8090"))
    # Secret token for accessing the web dashboard and /admin/* API endpoints.
    # Generate a random string: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
    DASHBOARD_TOKEN: str = os.getenv("DASHBOARD_TOKEN", "")
    # Public HTTPS URL of the web dashboard. Set this to enable the "Dashboard"
    # button in the Telegram chat menu. Must be HTTPS for Telegram Web Apps.
    # Example: https://bot.yourdomain.com or your Cloudflare tunnel URL.
    WEB_URL: str = os.getenv("WEB_URL", "")
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")
    # IANA timezone users enter/see times in. DB stores naive UTC.
    TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Tashkent")
    # Mini App short name from @BotFather (/newapp). When set, channel-post
    # join buttons become direct-link Mini App URL buttons
    # (t.me/<bot>/<short_name>?startapp=gw_<id>) instead of callback buttons.
    MINIAPP_SHORT_NAME: str = os.getenv("MINIAPP_SHORT_NAME", "")
    DB_POOL_SIZE: int = int(os.getenv("DB_POOL_SIZE", "20"))
    DB_MAX_OVERFLOW: int = int(os.getenv("DB_MAX_OVERFLOW", "30"))
    # Max updates processed concurrently. Removes the default sequential
    # bottleneck so a burst (e.g. thousands of /start at once) drains quickly.
    # Keep this in a sensible ratio to the DB pool (POOL_SIZE + MAX_OVERFLOW).
    MAX_CONCURRENT_UPDATES: int = int(os.getenv("MAX_CONCURRENT_UPDATES", "256"))
    # Comma-separated channels a referred user must join for the referral to count.
    # Leave empty to only require the referred user to be a real (non-bot) account.
    REFERRAL_REQUIRED_CHANNELS: list = field(default_factory=lambda: [
        c.strip() for c in os.getenv("REFERRAL_REQUIRED_CHANNELS", "").split(",") if c.strip()
    ])

    # ─── Webhook mode (self-hosted; alternative to polling) ──────────────────
    # Polling is fine for dev/small bots. For production/scale, run behind your
    # own HTTPS reverse proxy (Caddy/nginx + free Let's Encrypt cert) — no paid
    # webhook service needed.
    USE_WEBHOOK: bool = os.getenv("USE_WEBHOOK", "false").lower() in ("1", "true", "yes")
    # Public HTTPS base URL Telegram will call, e.g. https://bot.example.com
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
    # Interface/port the bot's internal webhook server listens on (behind the proxy).
    WEBHOOK_LISTEN: str = os.getenv("WEBHOOK_LISTEN", "0.0.0.0")
    WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8443"))
    # URL path segment appended to WEBHOOK_URL (keep it hard-to-guess).
    WEBHOOK_PATH: str = os.getenv("WEBHOOK_PATH", "telegram")
    # Optional shared secret; Telegram echoes it in a header so you can reject
    # any request that didn't come from Telegram. Highly recommended.
    WEBHOOK_SECRET_TOKEN: str = os.getenv("WEBHOOK_SECRET_TOKEN", "")


settings = Settings()
