"""Common bot handlers (start, help, language selection)."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot.i18n import SUPPORTED_LANGUAGES, get_text
from bot.config import settings
from bot.utils.lang import get_user_lang, set_user_lang, t
from bot.utils.referral import parse_referral_payload, process_referral
from bot.handlers.captcha_handler import is_user_verified

# ─── Reply Keyboard Menu (persistent buttons at the bottom) ──────────────────


def get_main_menu_keyboard(lang: str = "uz") -> ReplyKeyboardMarkup:
    """Build the main menu reply keyboard based on language."""
    menus = {
        "uz": [
            ["🎲 Yutuqli o'yin yaratish", "📋 Mening o'yinlarim"],
            ["🏅 Konkurs yaratish", "🏆 Reyting"],
            ["👥 Do'st taklif qilish", "🚪 Join filter"],
            ["⚙️ Sozlamalar"],
        ],
        "ru": [
            ["🎲 Создать розыгрыш", "📋 Мои розыгрыши"],
            ["🏅 Создать конкурс", "🏆 Рейтинг"],
            ["👥 Пригласить друга", "🚪 Join filter"],
            ["⚙️ Настройки"],
        ],
        "en": [
            ["🎲 Create Giveaway", "📋 My Giveaways"],
            ["🏅 Create Contest", "🏆 Leaderboard"],
            ["👥 Invite Friends", "🚪 Join Filter"],
            ["⚙️ Settings"],
        ],
    }
    buttons = menus.get(lang, menus["uz"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

logger = logging.getLogger(__name__)

# Language display names
LANG_NAMES = {
    "en": "🇬🇧 English",
    "ru": "🇷🇺 Русский",
    "uz": "🇺🇿 O'zbekcha",
}


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command, including referral deep links and CAPTCHA.

    Optimized for bursts (e.g. a referral konkurs sending thousands of /start
    at once): the welcome reply is sent immediately, and referral bookkeeping
    is done afterwards with the network membership check deferred, so /start
    never makes a per-user Telegram API call and never fails on referral errors.
    """
    user = update.effective_user
    user_id = user.id

    # Always respond first so users get instant feedback, even under heavy load.
    text = await t("welcome", user_id)
    lang = await get_user_lang(user_id)
    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=get_main_menu_keyboard(lang)
    )

    # Referral deep link: /start ref_<referrer_id>[_<giveaway_id>]
    # Recorded with verify_subscription=False → pure DB, no Telegram call here.
    if context.args:
        payload = context.args[0]
        # Handle /start verify (from captcha prompt button)
        if payload == "verify":
            from bot.handlers.captcha_handler import send_captcha
            await send_captcha(update, context)
            return

        referrer_id, giveaway_id = parse_referral_payload(payload)
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

    # If user is not CAPTCHA-verified, prompt them
    if not await is_user_verified(user_id):
        bot_username = context.bot.username
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔒 Tekshiruvdan o'tish (CAPTCHA)",
                url=f"https://t.me/{bot_username}?start=verify"
            )]
        ])
        await update.message.reply_text(
            "🔒 <b>Qatnashish uchun tekshiruv kerak</b>\n\n"
            "Botlardan himoyalanish uchun oddiy matematik misolni yeching.\n"
            "Bu bir marta — keyin barcha funksiyalar ochiq bo'ladi.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )


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

    # Update the reply keyboard to match new language
    try:
        await context.bot.send_message(
            user_id, "✅",
            reply_markup=get_main_menu_keyboard(lang_code),
        )
    except Exception:
        pass


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


# ─── Reply Keyboard Menu Handlers ────────────────────────────────────────────


async def menu_create_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle '🎲 Create Giveaway' button tap."""
    web_url = settings.WEB_URL
    if web_url:
        await open_miniapp_tab(update, context, "create")
    else:
        # Fallback: trigger /newgiveaway command
        from bot.handlers.giveaway import new_giveaway_start
        await new_giveaway_start(update, context)


async def menu_my_giveaways(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle '📋 My Giveaways' button tap."""
    web_url = settings.WEB_URL
    if web_url:
        await open_miniapp_tab(update, context, "games")
    else:
        from bot.handlers.giveaway import my_giveaways
        await my_giveaways(update, context)


async def menu_create_contest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle '🏅 Create Contest' button tap."""
    web_url = settings.WEB_URL
    if web_url:
        await open_miniapp_tab(update, context, "create")
    else:
        from bot.handlers.contest import new_contest_start
        await new_contest_start(update, context)


async def menu_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle '🏆 Leaderboard' button tap."""
    await open_miniapp_tab(update, context, "leaders")


async def menu_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle '👥 Invite Friends' button tap."""
    from bot.handlers.referral_handler import referral_command
    await referral_command(update, context)


async def menu_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle '⚙️ Settings' button tap."""
    web_url = settings.WEB_URL
    if web_url:
        await open_miniapp_tab(update, context, "profile")
    else:
        await lang_command(update, context)


async def menu_joinfilter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle '🚪 Join Filter' button tap."""
    from bot.handlers.join_request import joinfilter_command
    await joinfilter_command(update, context)


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
        # Reply keyboard button handlers (all 3 languages)
        MessageHandler(filters.Regex(r"^(🎲 Yutuqli o'yin yaratish|🎲 Создать розыгрыш|🎲 Create Giveaway)$"), menu_create_giveaway),
        MessageHandler(filters.Regex(r"^(📋 Mening o'yinlarim|📋 Мои розыгрыши|📋 My Giveaways)$"), menu_my_giveaways),
        MessageHandler(filters.Regex(r"^(🏅 Konkurs yaratish|🏅 Создать конкурс|🏅 Create Contest)$"), menu_create_contest),
        MessageHandler(filters.Regex(r"^(🏆 Reyting|🏆 Рейтинг|🏆 Leaderboard)$"), menu_leaderboard),
        MessageHandler(filters.Regex(r"^(👥 Do'st taklif qilish|👥 Пригласить друга|👥 Invite Friends)$"), menu_referral),
        MessageHandler(filters.Regex(r"^(🚪 Join filter|🚪 Join Filter)$"), menu_joinfilter),
        MessageHandler(filters.Regex(r"^(⚙️ Sozlamalar|⚙️ Настройки|⚙️ Settings)$"), menu_settings),
    ]
