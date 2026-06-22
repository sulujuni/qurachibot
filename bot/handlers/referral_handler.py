"""Referral system handler."""

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.utils.lang import get_user_lang
from bot.utils.referral import generate_referral_link, get_referral_count
from bot.utils.loyalty import POINTS_CONFIG


async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show referral link and stats. Command: /referral [giveaway_id]"""
    user = update.effective_user
    bot_username = context.bot.username

    giveaway_id = None
    if context.args:
        try:
            giveaway_id = int(context.args[0])
        except ValueError:
            pass

    link = generate_referral_link(bot_username, user.id, giveaway_id)
    count = await get_referral_count(user.id)

    gw_text = f" for giveaway #{giveaway_id}" if giveaway_id else ""

    text = (
        f"👥 <b>Your Referral Link{gw_text}</b>\n\n"
        f"🔗 <code>{link}</code>\n\n"
        f"Share this link with friends! When they start the bot through your link:\n"
        f"  • You earn <b>+{POINTS_CONFIG['referral']} loyalty points</b>\n"
        f"  • You get <b>+1 bonus entry</b> in giveaways\n\n"
        f"📊 <b>Your referral stats:</b>\n"
        f"  Total referrals: {count}\n"
        f"  Points earned: {count * POINTS_CONFIG['referral']}"
    )

    await update.message.reply_text(text, parse_mode="HTML")


def get_referral_handlers() -> list:
    """Return referral handlers."""
    return [
        CommandHandler("referral", referral_command),
    ]
