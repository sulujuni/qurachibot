"""Main entry point for the Telegram Giveaway & Contest Bot."""

import logging
import sys

from telegram.ext import Application

from bot.config import settings
from bot.handlers.common import get_common_handlers
from bot.handlers.contest import get_contest_handlers
from bot.handlers.giveaway import get_giveaway_handlers
from bot.models import init_db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Initialize the database on startup."""
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database initialized successfully.")


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
        .post_init(post_init)
        .build()
    )

    for handler in get_giveaway_handlers():
        application.add_handler(handler)
    for handler in get_contest_handlers():
        application.add_handler(handler)
    for handler in get_common_handlers():
        application.add_handler(handler)

    logger.info("Bot is ready! Starting polling...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
