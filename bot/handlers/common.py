"""Common bot handlers (start, help, language selection)."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.i18n import SUPPORTED_LANGUAGES, get_text
from bot.config import settings
from bot.utils.lang import get_user_lang, set_user_lang, t
from bot.utils.referral import parse_referral_payload, process_referral

logger = logging.getLogger(__name__)

# Language display names
LANG_NAMES = {
    "en": "🇬🇧 English",
    "ru": "🇷🇺 Русский",
    "uz": "🇺🇿 O'zbekcha",
}


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command, including referral deep links.

    Optimized for bursts (e.g. a referral konkurs sending thousands of /start
    at once): the welcome reply is sent immediately, and referral bookkeeping
    is done afterwards with the network membership check deferred, so /start
    never makes a per-user Telegram API call and never fails on referral errors.
    """
    user = update.effective_user
    user_id = user.id

    # Always respond first so users get instant feedback, even under heavy load.
    text = await t("welcome", user_id)
    await update.message.reply_text(text, parse_mode="HTML")

    # Referral deep link: /start ref_<referrer_id>[_<giveaway_id>]
    # Recorded with verify_subscription=False → pure DB, no Telegram call here.
    if context.args:
        referrer_id, giveaway_id = parse_referral_payload(context.args[0])
        if referrer_id:
            try:
                await process_referral(
                    context.bot,
                    referrer_id,
                    user,
                    giveaway_id=giveaway_id or None,
                    verify_subscription=False,
                )
            except Exception as e:
                logger.warning("Referral processing failed for %s: %s", user_id, e)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    user_id = update.effective_user.id
    text = await t("help", user_id)
    await update.message.reply_text(text, parse_mode="HTML")


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /lang command — show language selection."""
    user_id = update.effective_user.id
    text = await t("lang_select", user_id)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(name, callback_data=f"setlang_{code}")]
        for code, name in LANG_NAMES.items()
    ])

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle language selection callback."""
    query = update.callback_query
    lang_code = query.data.split("_")[1]
    user_id = query.from_user.id

    if lang_code not in SUPPORTED_LANGUAGES:
        await query.answer("Unknown language", show_alert=True)
        return

    await set_user_lang(user_id, lang_code)
    await query.answer()

    lang_name = LANG_NAMES.get(lang_code, lang_code)
    text = get_text("lang_set", lang=lang_code, lang_name=lang_name)
    await query.edit_message_text(text, parse_mode="HTML")


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the Mini App. Command: /dashboard"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    web_url = settings.WEB_URL
    if not web_url:
        await update.message.reply_text(
            "📊 Mini App hali sozlanmagan.\n"
            "<code>WEB_URL</code> ni .env faylida kiriting.",
            parse_mode="HTML",
        )
        return

    miniapp_url = f"{web_url.rstrip('/')}/miniapp"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Qurachi ilovasini ochish", web_app=WebAppInfo(url=miniapp_url))]
    ])
    await update.message.reply_text(
        "🎲 <b>Qurachi Mini App</b>\n\n"
        "Yutuqli o'yinlar, konkurslar, reyting va boshqa hamma narsa — bir joyda.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def open_miniapp_tab(update: Update, context: ContextTypes.DEFAULT_TYPE, tab: str) -> None:
    """Helper to open the Mini App on a specific tab."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    web_url = settings.WEB_URL
    if not web_url:
        await update.message.reply_text("Mini App hali sozlanmagan.")
        return

    url = f"{web_url.rstrip('/')}/miniapp?tab={tab}"
    labels = {"games": "🎮 O'yinlarim", "leaders": "🏆 Reyting", "create": "➕ Yaratish"}
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(labels.get(tab, "🎲 Ochish"), web_app=WebAppInfo(url=url))]
    ])
    await update.message.reply_text("👆 Tugmani bosing:", reply_markup=keyboard, parse_mode="HTML")


async def mygames_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open My Games tab. Command: /mygiveaways"""
    await open_miniapp_tab(update, context, "games")


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open Leaderboard tab. Command: /leaderboard"""
    await open_miniapp_tab(update, context, "leaders")


def get_common_handlers() -> list:
    """Return common command handlers."""
    return [
        CommandHandler("start", start_command),
        CommandHandler("help", help_command),
        CommandHandler("dashboard", dashboard_command),
        CommandHandler("mygiveaways", mygames_command),
        CommandHandler("leaderboard", leaderboard_command),
        CommandHandler("lang", lang_command),
        CallbackQueryHandler(lang_callback, pattern=r"^setlang_"),
    ]
