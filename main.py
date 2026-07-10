"""Main entry point for the Telegram Giveaway & Contest Bot."""

import logging
import sys
from datetime import timedelta

from telegram import Update
from telegram.ext import Application

from bot.config import settings
from bot.handlers.admin import get_admin_handlers
from bot.handlers.alerts import get_alert_handlers
from bot.handlers.common import get_common_handlers, get_captcha_answer_handler
from bot.handlers.contest import get_contest_handlers
from bot.handlers.giveaway import get_giveaway_handlers
from bot.handlers.group_giveaway import get_group_giveaway_handlers, _load_active_giveaways
from bot.handlers.join_request import get_join_request_handlers
from bot.handlers.comment_randomizer import get_comment_randomizer_handlers
from bot.handlers.captcha_handler import get_captcha_handlers
from bot.handlers.loyalty_handler import get_loyalty_handlers
from bot.handlers.referral_handler import get_referral_handlers
from bot.jobs import (
    check_expired_giveaways,
    check_expired_group_giveaways,
    check_submission_deadlines,
    publish_queued_giveaways,
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


async def error_handler(update: object, context) -> None:
    """Global error handler — logs the error and notifies the user."""
    logger.error("Unhandled exception:", exc_info=context.error)

    # Try to notify the user
    if update and hasattr(update, "effective_user") and update.effective_user:
        user_id = update.effective_user.id
        try:
            from bot.utils.lang import get_user_lang
            lang = await get_user_lang(user_id)
        except Exception:
            lang = "uz"

        error_msgs = {
            "uz": "⚠️ Kechirasiz, kutilmagan xatolik yuz berdi.\n\n🐛 Agar muammo davom etsa, \"🐛 Xatolik xabar qilish\" tugmasini bosing.",
            "ru": "⚠️ Произошла непредвиденная ошибка.\n\n🐛 Если проблема повторяется, нажмите \"🐛 Сообщить об ошибке\".",
            "en": "⚠️ An unexpected error occurred.\n\n🐛 If the problem persists, tap \"🐛 Report Bug\".",
        }
        try:
            if hasattr(update, "effective_chat") and update.effective_chat:
                await context.bot.send_message(
                    update.effective_chat.id,
                    error_msgs.get(lang, error_msgs["uz"]),
                )
        except Exception:
            pass

    # Notify admins with error details
    import traceback
    tb = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
    error_text = f"🚨 <b>Bot Error</b>\n\n<pre>{tb[:3000]}</pre>"
    for admin_id in settings.ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, error_text, parse_mode="HTML")
        except Exception:
            pass


async def post_init(application: Application) -> None:
    """Initialize database, set bot commands, and schedule jobs."""
    logger.info("Initializing database...")
    logger.info(f"Database: {settings.DATABASE_URL.split('@')[-1] if '@' in settings.DATABASE_URL else settings.DATABASE_URL}")
    await init_db()
    logger.info("Database initialized successfully (PostgreSQL).")

    # ─── Auto-run pending migrations via SQLAlchemy (no psql needed) ───────
    try:
        from sqlalchemy import text as sql_text
        from bot.models.database import async_session
        async with async_session() as session:
            migrations = [
                # Convert status from ENUM to VARCHAR (one-time, safe to re-run)
                "ALTER TABLE giveaways ALTER COLUMN status TYPE VARCHAR(20) USING status::text",
                "ALTER TABLE giveaways ALTER COLUMN status SET DEFAULT 'active'",
                "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS post_text TEXT",
                "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS post_file_id VARCHAR(500)",
                "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS post_media_type VARCHAR(20)",
                "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS is_test BOOLEAN DEFAULT FALSE",
                "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS channel_id BIGINT",
                "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS message_id BIGINT",
                "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS scheduled_start TIMESTAMP",
                "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS published_at TIMESTAMP",
                "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS button_label VARCHAR(100)",
                "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS boost_channels TEXT",
                "ALTER TABLE group_giveaways ADD COLUMN IF NOT EXISTS button_label VARCHAR(100)",
                "ALTER TABLE group_giveaways ADD COLUMN IF NOT EXISTS boost_channels TEXT",
                "ALTER TABLE giveaways ALTER COLUMN prize DROP NOT NULL",
                "ALTER TABLE group_giveaways ADD COLUMN IF NOT EXISTS post_text TEXT",
                "ALTER TABLE group_giveaways ADD COLUMN IF NOT EXISTS post_file_id VARCHAR(500)",
                "ALTER TABLE group_giveaways ADD COLUMN IF NOT EXISTS post_media_type VARCHAR(20)",
                "ALTER TABLE group_giveaways ALTER COLUMN prize DROP NOT NULL",
                "ALTER TABLE contests ADD COLUMN IF NOT EXISTS post_text TEXT",
                "ALTER TABLE contests ADD COLUMN IF NOT EXISTS post_file_id VARCHAR(500)",
                "ALTER TABLE contests ADD COLUMN IF NOT EXISTS post_media_type VARCHAR(20)",
                """CREATE TABLE IF NOT EXISTS user_channels (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    chat_title VARCHAR(255),
                    chat_username VARCHAR(255),
                    added_at TIMESTAMP DEFAULT NOW()
                )""",
                "CREATE INDEX IF NOT EXISTS ix_user_channels_user_id ON user_channels(user_id)",
            ]
            for stmt in migrations:
                try:
                    await session.execute(sql_text(stmt))
                except Exception:
                    pass  # Column may already exist or table may not exist yet
            await session.commit()
        logger.info("Database migrations applied.")
    except Exception as e:
        logger.warning("Migration step failed (non-fatal): %s", e)

    # Load active group giveaway posts into memory
    await _load_active_giveaways()
    logger.info("Loaded active group giveaway posts.")

    # ─── Set bot command menu (the "/" button in chats), per language ───────
    from telegram import BotCommand, MenuButtonWebApp, WebAppInfo

    command_sets = {
        # Default (English) — shown to any language without a specific set
        None: [
            BotCommand("start", "Start the bot"),
            BotCommand("help", "List of all commands"),
            BotCommand("newgiveaway", "Create a giveaway (prize draw)"),
            BotCommand("newcontest", "Create a contest"),
            BotCommand("groupgiveaway", "Comment-to-enter giveaway in a group"),
            BotCommand("channelgiveaway", "Giveaway for a channel"),
            BotCommand("draw", "Draw giveaway winners"),
            BotCommand("groupdraw", "Draw group giveaway winners"),
            BotCommand("mygiveaways", "Your giveaways"),
            BotCommand("mycontests", "Your contests"),
            BotCommand("referral", "Your invite link & stats"),
            BotCommand("points", "View your loyalty points"),
            BotCommand("leaderboard", "Top users"),
            BotCommand("lang", "Change language"),
        ],
        "ru": [
            BotCommand("start", "Запустить бота"),
            BotCommand("help", "Список всех команд"),
            BotCommand("newgiveaway", "Создать розыгрыш"),
            BotCommand("newcontest", "Создать конкурс"),
            BotCommand("groupgiveaway", "Розыгрыш по комментариям в группе"),
            BotCommand("channelgiveaway", "Розыгрыш для канала"),
            BotCommand("draw", "Определить победителей розыгрыша"),
            BotCommand("groupdraw", "Определить победителей в группе"),
            BotCommand("mygiveaways", "Мои розыгрыши"),
            BotCommand("mycontests", "Мои конкурсы"),
            BotCommand("referral", "Реферальная ссылка и статистика"),
            BotCommand("points", "Ваши баллы"),
            BotCommand("leaderboard", "Рейтинг пользователей"),
            BotCommand("lang", "Сменить язык"),
        ],
        "uz": [
            BotCommand("start", "Botni ishga tushirish"),
            BotCommand("help", "Barcha buyruqlar ro'yxati"),
            BotCommand("newgiveaway", "Yutuqli o'yin (sovg'a o'ynatish)"),
            BotCommand("newcontest", "Konkurs yaratish"),
            BotCommand("groupgiveaway", "Guruhda izohli yutuqli o'yin"),
            BotCommand("channelgiveaway", "Kanal uchun yutuqli o'yin"),
            BotCommand("draw", "Yutuqli o'yin g'oliblarini aniqlash"),
            BotCommand("groupdraw", "Guruh o'yini g'oliblarini aniqlash"),
            BotCommand("mygiveaways", "Mening yutuqli o'yinlarim"),
            BotCommand("mycontests", "Mening konkurslarim"),
            BotCommand("referral", "Do'st taklif qilish havolasi"),
            BotCommand("points", "Ballaringizni ko'rish"),
            BotCommand("leaderboard", "Eng faol foydalanuvchilar"),
            BotCommand("lang", "Tilni o'zgartirish"),
        ],
    }
    for lang_code, cmds in command_sets.items():
        if lang_code:
            await application.bot.set_my_commands(cmds, language_code=lang_code)
        else:
            await application.bot.set_my_commands(cmds)
    logger.info("Bot command menus set for %d languages.", len(command_sets))

    # ─── Set web dashboard menu button (opens Mini App in Telegram) ─────────
    # Only set if WEB_URL is configured (the dashboard must be on HTTPS).
    web_url = settings.WEB_URL
    if web_url:
        try:
            miniapp_url = f"{web_url.rstrip('/')}/miniapp"
            menu_button = MenuButtonWebApp(
                text="🎲 Qurachi",
                web_app=WebAppInfo(url=miniapp_url),
            )
            await application.bot.set_chat_menu_button(menu_button=menu_button)
            logger.info("Web App menu button set: %s", miniapp_url)
        except Exception as e:
            logger.warning("Failed to set menu button (need HTTPS URL): %s", e)
    else:
        logger.info("WEB_URL not set — skipping Mini App menu button.")

    # Schedule recurring jobs
    job_queue = application.job_queue
    if job_queue:
        # Publish queued giveaways every 10 seconds (scheduled start time)
        job_queue.run_repeating(publish_queued_giveaways, interval=10, first=5)
        # Check expired giveaways every 60 seconds (auto-draw at end time)
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
    for handler in get_join_request_handlers():
        application.add_handler(handler)
    for handler in get_comment_randomizer_handlers():
        application.add_handler(handler)
    for handler in get_captcha_handlers():
        application.add_handler(handler)
    for handler in get_alert_handlers():
        application.add_handler(handler)
    for handler in get_common_handlers():
        application.add_handler(handler)

    # CAPTCHA answer handler in group 1 (lower priority than all other handlers)
    # This ensures it only catches messages when no other handler matched
    application.add_handler(get_captcha_answer_handler(), group=1)

    # Global error handler — catches unhandled exceptions, notifies user + admin
    application.add_error_handler(error_handler)

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
