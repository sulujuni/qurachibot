"""Referral system handler."""

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.i18n import get_text
from bot.utils.lang import get_user_lang
from bot.utils.referral import generate_referral_link, get_referral_count
from bot.utils.loyalty import POINTS_CONFIG


async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show referral link and stats. Command: /referral [giveaway_id]"""
    user = update.effective_user
    lang = await get_user_lang(user.id)
    bot_username = context.bot.username

    giveaway_id = None
    if context.args:
        try:
            giveaway_id = int(context.args[0])
        except ValueError:
            pass

    link = generate_referral_link(bot_username, user.id, giveaway_id)
    count = await get_referral_count(user.id)  # verified referrals only

    gw_text = f" #{giveaway_id}" if giveaway_id else ""

    text = get_text(
        "ref_info", lang=lang,
        gw_text=gw_text,
        link=link,
        points=POINTS_CONFIG["referral"],
        count=count,
        earned=count * POINTS_CONFIG["referral"],
    )

    await update.message.reply_text(text, parse_mode="HTML")


def get_referral_handlers() -> list:
    """Return referral handlers."""
    return [
        CommandHandler("referral", referral_command),
    ]
