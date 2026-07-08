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
            ["👥 Do'st taklif qilish", "🚪 So'rovlarni boshqarish"],
            ["⚙️ Sozlamalar"],
        ],
        "ru": [
            ["🎲 Создать розыгрыш", "📋 Мои розыгрыши"],
            ["🏅 Создать конкурс", "🏆 Рейтинг"],
            ["👥 Пригласить друга", "🚪 Управление заявками"],
            ["⚙️ Настройки"],
        ],
        "en": [
            ["🎲 Create Giveaway", "📋 My Giveaways"],
            ["🏅 Create Contest", "🏆 Leaderboard"],
            ["👥 Invite Friends", "🚪 Join Requests"],
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

    # If user is not CAPTCHA-verified, send CAPTCHA directly (no button needed)
    if not await is_user_verified(user_id):
        captcha = None
        from bot.utils.captcha import generate_captcha
        captcha = generate_captcha()
        context.user_data["captcha_answer"] = captcha.answer
        context.user_data["captcha_attempts"] = 0
        context.user_data["awaiting_captcha"] = True

        await update.message.reply_text(
            f"🔒 <b>Tekshiruv (CAPTCHA)</b>\n\n"
            f"Botlardan himoyalanish uchun oddiy misolni yeching.\n"
            f"Bu bir marta — keyin barcha funksiyalar ochiq.\n\n"
            f"🧮 <code>{captcha.question}</code>\n\n"
            f"Javobni raqam bilan yuboring:",
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
    """Handle '🚪 Join Requests' button tap — shows explanation + add to channel."""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    bot_username = context.bot.username

    texts = {
        "uz": (
            "🚪 <b>Kanalga qo'shilish so'rovlarini boshqarish</b>\n\n"
            "Bu funksiya yordamida yopiq kanal yoki guruhga kelib tushadigan "
            "barcha <b>qo'shilish so'rovlarini avtomatik qabul qilish</b> mumkin.\n\n"
            "📌 <b>Qanday ishlaydi:</b>\n"
            "1. Botni kanalingizga <b>admin</b> sifatida qo'shing\n"
            "2. Kanalda yozing: <code>/joinfilter all</code>\n"
            "3. Tayyor! Bot barcha so'rovlarni avtomatik qabul qiladi\n\n"
            "🔸 <b>Qo'shimcha rejimlar:</b>\n"
            "• <code>/joinfilter females</code> — faqat ayollar\n"
            "• <code>/joinfilter males</code> — faqat erkaklar\n"
            "• <code>/joinfilter subscribed @kanal</code> — obuna shart\n"
            "• <code>/joinfilter premium</code> — faqat Premium\n"
            "• <code>/joinfilter off</code> — o'chirish\n\n"
            "⚠️ <i>females/males rejimlar faqat botda CAPTCHA o'tgan foydalanuvchilarga ishlaydi. "
            "Qolganlari uchun \"all\" rejimi eng mos.</i>"
        ),
        "ru": (
            "🚪 <b>Управление заявками на вступление</b>\n\n"
            "Эта функция автоматически принимает все <b>заявки на вступление</b> "
            "в ваш закрытый канал или группу.\n\n"
            "📌 <b>Как это работает:</b>\n"
            "1. Добавьте бота в канал как <b>администратора</b>\n"
            "2. Напишите в канале: <code>/joinfilter all</code>\n"
            "3. Готово! Бот автоматически принимает все заявки\n\n"
            "🔸 <b>Дополнительные режимы:</b>\n"
            "• <code>/joinfilter females</code> — только девушки\n"
            "• <code>/joinfilter males</code> — только парни\n"
            "• <code>/joinfilter subscribed @канал</code> — подписчики\n"
            "• <code>/joinfilter premium</code> — только Premium\n"
            "• <code>/joinfilter off</code> — выключить\n\n"
            "⚠️ <i>Режимы females/males работают только для пользователей, "
            "прошедших CAPTCHA в боте. Для остальных подойдёт режим \"all\".</i>"
        ),
        "en": (
            "🚪 <b>Manage Join Requests</b>\n\n"
            "This feature automatically accepts all <b>join requests</b> "
            "to your private channel or group.\n\n"
            "📌 <b>How it works:</b>\n"
            "1. Add the bot to your channel as an <b>admin</b>\n"
            "2. Type in the channel: <code>/joinfilter all</code>\n"
            "3. Done! Bot auto-accepts all join requests\n\n"
            "🔸 <b>Additional modes:</b>\n"
            "• <code>/joinfilter females</code> — females only\n"
            "• <code>/joinfilter males</code> — males only\n"
            "• <code>/joinfilter subscribed @channel</code> — subscribers\n"
            "• <code>/joinfilter premium</code> — Premium only\n"
            "• <code>/joinfilter off</code> — disable\n\n"
            "⚠️ <i>females/males modes only work for users who passed CAPTCHA "
            "in the bot. For everyone else, use \"all\" mode.</i>"
        ),
    }

    text = texts.get(lang, texts["uz"])

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "➕ Kanalga/guruhga qo'shish" if lang == "uz"
            else "➕ Добавить в канал/группу" if lang == "ru"
            else "➕ Add to channel/group",
            url=f"https://t.me/{bot_username}?startgroup=true&admin=invite_users"
        )],
    ])

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def handle_captcha_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle CAPTCHA answer from /start flow (standalone, no ConversationHandler).
    
    IMPORTANT: This handler only processes messages when user_data has 
    'awaiting_captcha' = True. Otherwise it does NOTHING (passes through).
    """
    if not context.user_data.get("awaiting_captcha"):
        return  # Not waiting for captcha — let other handlers process this

    from bot.utils.captcha import generate_captcha, verify_captcha
    from bot.handlers.captcha_handler import mark_verified

    user_id = update.effective_user.id
    user_answer = update.message.text.strip()
    correct_answer = context.user_data.get("captcha_answer")
    attempts = context.user_data.get("captcha_attempts", 0)

    if correct_answer is None:
        context.user_data.pop("awaiting_captcha", None)
        return

    if verify_captcha(user_answer, correct_answer):
        # Correct! Ask gender
        context.user_data.pop("captcha_answer", None)
        context.user_data.pop("captcha_attempts", None)
        context.user_data["awaiting_captcha"] = False
        context.user_data["awaiting_gender"] = True

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👨 Erkak / Male", callback_data="gender_male"),
                InlineKeyboardButton("👩 Ayol / Female", callback_data="gender_female"),
            ]
        ])
        await update.message.reply_text(
            "✅ <b>To'g'ri!</b>\n\n"
            "Oxirgi savol — jinsingizni tanlang:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    else:
        attempts += 1
        context.user_data["captcha_attempts"] = attempts
        if attempts >= 3:
            captcha = generate_captcha()
            context.user_data["captcha_answer"] = captcha.answer
            context.user_data["captcha_attempts"] = 0
            await update.message.reply_text(
                f"❌ 3 ta noto'g'ri javob. Yangi misol:\n\n"
                f"🧮 <code>{captcha.question}</code>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"❌ Noto'g'ri. Yana urinib ko'ring ({3 - attempts} qoldi):",
            )


async def handle_gender_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle gender selection from /start captcha flow."""
    query = update.callback_query
    if not context.user_data.get("awaiting_gender"):
        return

    await query.answer()
    user_id = query.from_user.id
    gender = query.data.split("_")[1]

    from bot.handlers.captcha_handler import mark_verified
    await mark_verified(user_id, gender=gender)
    context.user_data["awaiting_gender"] = False

    gender_emoji = "👨" if gender == "male" else "👩"
    lang = await get_user_lang(user_id)
    await query.edit_message_text(
        f"✅ <b>Tasdiqlandi!</b> {gender_emoji}\n\n"
        "Barcha funksiyalar sizga ochiq!",
        parse_mode="HTML",
    )
    # Send main menu
    await context.bot.send_message(
        user_id,
        "Davom etish uchun quyidagi tugmalardan foydalaning:",
        reply_markup=get_main_menu_keyboard(lang),
    )

    # Auto-approve any pending join requests
    pending = context.bot_data.get("pending_joins", {}).pop(user_id, None)
    if pending:
        try:
            await context.bot.approve_chat_join_request(
                chat_id=pending["chat_id"], user_id=user_id,
            )
            await context.bot.send_message(
                user_id, "✅ Kanalga qo'shilish so'rovingiz qabul qilindi!",
            )
        except Exception:
            pass


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
        # Gender callback from /start captcha flow
        CallbackQueryHandler(handle_gender_callback, pattern=r"^gender_(male|female)$"),
        # Reply keyboard button handlers (all 3 languages)
        MessageHandler(filters.Regex(r"^(🎲 Yutuqli o'yin yaratish|🎲 Создать розыгрыш|🎲 Create Giveaway)$"), menu_create_giveaway),
        MessageHandler(filters.Regex(r"^(📋 Mening o'yinlarim|📋 Мои розыгрыши|📋 My Giveaways)$"), menu_my_giveaways),
        MessageHandler(filters.Regex(r"^(🏅 Konkurs yaratish|🏅 Создать конкурс|🏅 Create Contest)$"), menu_create_contest),
        MessageHandler(filters.Regex(r"^(🏆 Reyting|🏆 Рейтинг|🏆 Leaderboard)$"), menu_leaderboard),
        MessageHandler(filters.Regex(r"^(👥 Do'st taklif qilish|👥 Пригласить друга|👥 Invite Friends)$"), menu_referral),
        MessageHandler(filters.Regex(r"^(🚪 So'rovlarni boshqarish|🚪 Управление заявками|🚪 Join Requests|🚪 Join filter|🚪 Join Filter)$"), menu_joinfilter),
        MessageHandler(filters.Regex(r"^(⚙️ Sozlamalar|⚙️ Настройки|⚙️ Settings)$"), menu_settings),
    ]


def get_captcha_answer_handler():
    """Return the CAPTCHA answer handler separately — must be added in a LATER group."""
    return MessageHandler(filters.TEXT & ~filters.COMMAND, handle_captcha_answer)
