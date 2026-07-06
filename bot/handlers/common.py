"""Common bot handlers (start, help, language selection)."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.i18n import SUPPORTED_LANGUAGES, get_text
from bot.utils.lang import get_user_lang, set_user_lang, t
from bot.utils.referral import parse_referral_payload, process_referral

# Language display names
LANG_NAMES = {
    "en": "🇬🇧 English",
    "ru": "🇷🇺 Русский",
    "uz": "🇺🇿 O'zbekcha",
}


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command, including referral deep links."""
    user = update.effective_user
    user_id = user.id
    lang = await get_user_lang(user_id)

    # Process referral deep link: /start ref_<referrer_id>[_<giveaway_id>]
    if context.args:
        referrer_id, giveaway_id = parse_referral_payload(context.args[0])
        if referrer_id:
            status = await process_referral(
                context.bot,
                referrer_id,
                user,
                giveaway_id=giveaway_id or None,
            )
            if status == "pending":
                await update.message.reply_text(
                    get_text("ref_pending", lang=lang), parse_mode="HTML"
                )

    text = await t("welcome", user_id)
    await update.message.reply_text(text, parse_mode="HTML")


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


def get_common_handlers() -> list:
    """Return common command handlers."""
    return [
        CommandHandler("start", start_command),
        CommandHandler("help", help_command),
        CommandHandler("lang", lang_command),
        CallbackQueryHandler(lang_callback, pattern=r"^setlang_"),
    ]
