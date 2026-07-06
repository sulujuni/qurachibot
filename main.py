"""Main entry point for the Telegram Giveaway & Contest Bot."""

import logging
import sys
from datetime import timedelta

from telegram import Update
from telegram.ext import Application

from bot.config import settings
from bot.handlers.admin import get_admin_handlers
from bot.handlers.alerts import get_alert_handlers
from bot.handlers.common import get_common_handlers
from bot.handlers.contest import get_contest_handlers
from bot.handlers.giveaway import get_giveaway_handlers
from bot.handlers.group_giveaway import get_group_giveaway_handlers, _load_active_giveaways
from bot.handlers.loyalty_handler import get_loyalty_handlers
from bot.handlers.referral_handler import get_referral_handlers
from bot.jobs import (
    check_expired_giveaways,
    check_expired_group_giveaways,
    check_submission_deadlines,
    send_new_event_alerts,
    send_reminders,
)
from bot.models import init_db
from bot.models.database import close_db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Initialize database and schedule jobs."""
    logger.info("Initializing database...")
    logger.info(f"Database: {settings.DATABASE_URL.split('@')[-1] if '@' in settings.DATABASE_URL else settings.DATABASE_URL}")
    await init_db()
    logger.info("Database initialized successfully (PostgreSQL).")

    # Load active group giveaway posts into memory
    await _load_active_giveaways()
    logger.info("Loaded active group giveaway posts.")

    # Schedule recurring jobs
    job_queue = application.job_queue
    if job_queue:
        # Check expired giveaways every 60 seconds
        job_queue.run_repeating(check_expired_giveaways, interval=60, first=10)
        # Check expired group/channel comment giveaways every 60 seconds
        job_queue.run_repeating(check_expired_group_giveaways, interval=60, first=20)
        # Check contest submission deadlines every 60 seconds
        job_queue.run_repeating(check_submission_deadlines, interval=60, first=15)
        # Send ending-soon reminders every 5 minutes
        job_queue.run_repeating(send_reminders, interval=300, first=30)
        # Send new event alerts every 5 minutes
        job_queue.run_repeating(send_new_event_alerts, interval=300, first=60)
        logger.info("Scheduled jobs registered.")


async def post_shutdown(application: Application) -> None:
    """Clean up database connections on shutdown."""
    logger.info("Closing database connections...")
    await close_db()
    logger.info("Database connections closed.")


def main() -> None:
    """Build and run the bot application."""
    if not settings.BOT_TOKEN or settings.BOT_TOKEN == "your_bot_token_here":
        logger.error(
            "BOT_TOKEN is not set! Set it in .env or environment.\n"
            "Get a token from @BotFather on Telegram."
        )
        sys.exit(1)

    logger.info("Starting Giveaway & Contest Bot...")

    application = (
        Application.builder()
        .token(settings.BOT_TOKEN)
        # Process updates concurrently instead of one-at-a-time, so a spike of
        # thousands of /start (e.g. a referral konkurs) is drained quickly.
        .concurrent_updates(settings.MAX_CONCURRENT_UPDATES)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register all handlers
    for handler in get_giveaway_handlers():
        application.add_handler(handler)
    for handler in get_contest_handlers():
        application.add_handler(handler)
    for handler in get_group_giveaway_handlers():
        application.add_handler(handler)
    for handler in get_admin_handlers():
        application.add_handler(handler)
    for handler in get_loyalty_handlers():
        application.add_handler(handler)
    for handler in get_referral_handlers():
        application.add_handler(handler)
    for handler in get_alert_handlers():
        application.add_handler(handler)
    for handler in get_common_handlers():
        application.add_handler(handler)

    # allowed_updates must include message reactions so REACTION-mode
    # giveaways can capture emoji reactions on posts.
    run_kwargs = dict(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

    if settings.USE_WEBHOOK:
        if not settings.WEBHOOK_URL:
            logger.error("USE_WEBHOOK=true but WEBHOOK_URL is not set. Aborting.")
            sys.exit(1)
        webhook_url = f"{settings.WEBHOOK_URL.rstrip('/')}/{settings.WEBHOOK_PATH}"
        logger.info(
            "Bot is ready! Starting webhook server on %s:%s (public: %s)",
            settings.WEBHOOK_LISTEN, settings.WEBHOOK_PORT, webhook_url,
        )
        # PTB runs its own lightweight HTTPS-less server here and calls
        # setWebhook for you. Terminate TLS at your reverse proxy in front.
        application.run_webhook(
            listen=settings.WEBHOOK_LISTEN,
            port=settings.WEBHOOK_PORT,
            url_path=settings.WEBHOOK_PATH,
            webhook_url=webhook_url,
            secret_token=settings.WEBHOOK_SECRET_TOKEN or None,
            **run_kwargs,
        )
    else:
        logger.info("Bot is ready! Starting polling...")
        application.run_polling(**run_kwargs)


if __name__ == "__main__":
    main()
