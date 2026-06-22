"""Notification, reminder, and alert handlers."""

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from sqlalchemy import select

from bot.models.database import async_session
from bot.models.notification import AlertSubscription
from bot.utils.lang import get_user_lang


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Subscribe to new giveaway/contest alerts. Command: /subscribe"""
    user = update.effective_user
    lang = await get_user_lang(user.id)

    async with async_session() as session:
        result = await session.execute(
            select(AlertSubscription).where(AlertSubscription.user_id == user.id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            await update.message.reply_text(
                "✅ You're already subscribed to alerts!\n"
                "Use /unsubscribe to stop receiving notifications."
            )
            return

        sub = AlertSubscription(
            user_id=user.id,
            username=user.username,
        )
        session.add(sub)
        await session.commit()

    await update.message.reply_text(
        "🔔 <b>Subscribed!</b>\n\n"
        "You'll be notified when:\n"
        "• New giveaways are created\n"
        "• New contests are created\n"
        "• Giveaways/contests are ending soon\n\n"
        "Use /unsubscribe to stop notifications.",
        parse_mode="HTML",
    )


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unsubscribe from alerts. Command: /unsubscribe"""
    user = update.effective_user

    async with async_session() as session:
        result = await session.execute(
            select(AlertSubscription).where(AlertSubscription.user_id == user.id)
        )
        sub = result.scalar_one_or_none()

        if not sub:
            await update.message.reply_text("ℹ️ You're not subscribed to alerts.")
            return

        await session.delete(sub)
        await session.commit()

    await update.message.reply_text("🔕 Unsubscribed from alerts. You won't receive notifications anymore.")


def get_alert_handlers() -> list:
    """Return alert subscription handlers."""
    return [
        CommandHandler("subscribe", subscribe_command),
        CommandHandler("unsubscribe", unsubscribe_command),
    ]
