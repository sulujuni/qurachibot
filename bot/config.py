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
    WEB_HOST: str = os.getenv("WEB_HOST", "0.0.0.0")
    WEB_PORT: int = int(os.getenv("WEB_PORT", "8080"))
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")
    DB_POOL_SIZE: int = int(os.getenv("DB_POOL_SIZE", "20"))
    DB_MAX_OVERFLOW: int = int(os.getenv("DB_MAX_OVERFLOW", "30"))
    # Comma-separated channels a referred user must join for the referral to count.
    # Leave empty to only require the referred user to be a real (non-bot) account.
    REFERRAL_REQUIRED_CHANNELS: list = field(default_factory=lambda: [
        c.strip() for c in os.getenv("REFERRAL_REQUIRED_CHANNELS", "").split(",") if c.strip()
    ])


settings = Settings()
