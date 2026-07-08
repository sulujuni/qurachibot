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
            "📌 <b>Qanday sozlash:</b>\n"
            "1. Pastdagi tugma orqali botni kanalga admin qiling\n"
            "2. \"✅ Qo'shdim\" tugmasini bosing\n"
            "3. Kanaldan istalgan <b>bitta xabarni forward</b> qiling\n"
            "4. Bot avtomatik sozlaydi!\n\n"
            "💡 <i>Yopiq kanallar uchun forward usuli ishlaydi — @username kerak emas.\n"
            "Kanal ID sini bilish uchun: kanaldan xabarni @userinfobot ga forward qiling.</i>\n\n"
            "👇 <b>Avval botni kanalga qo'shing:</b>"
        ),
        "ru": (
            "🚪 <b>Управление заявками на вступление</b>\n\n"
            "Эта функция автоматически принимает все <b>заявки на вступление</b> "
            "в ваш закрытый канал или группу.\n\n"
            "📌 <b>Как настроить:</b>\n"
            "1. Добавьте бота как админа (кнопка ниже)\n"
            "2. Нажмите \"✅ Добавил\"\n"
            "3. <b>Перешлите</b> любое сообщение из канала сюда\n"
            "4. Бот настроит автоматически!\n\n"
            "💡 <i>Для закрытых каналов — перешлите сообщение (без @username).\n"
            "Узнать ID канала: перешлите сообщение из канала боту @userinfobot</i>\n\n"
            "👇 <b>Сначала добавьте бота:</b>"
        ),
        "en": (
            "🚪 <b>Manage Join Requests</b>\n\n"
            "This feature automatically accepts all <b>join requests</b> "
            "to your private channel or group.\n\n"
            "📌 <b>How to set up:</b>\n"
            "1. Add the bot as admin (button below)\n"
            "2. Tap \"✅ Added\"\n"
            "3. <b>Forward</b> any message from your channel here\n"
            "4. Bot will configure automatically!\n\n"
            "💡 <i>Works for private channels — no @username needed.\n"
            "To find channel ID: forward a message to @userinfobot</i>\n\n"
            "👇 <b>First, add the bot:</b>"
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
        [InlineKeyboardButton(
            "✅ Qo'shdim, davom etish" if lang == "uz"
            else "✅ Добавил, продолжить" if lang == "ru"
            else "✅ Added, continue",
            callback_data="jf_setup_start"
        )],
    ])

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


# ─── Join Filter interactive setup (button-based) ────────────────────────────


async def jf_setup_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User tapped 'Added, continue' — ask to forward a message from the channel."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = await get_user_lang(user_id)

    texts = {
        "uz": (
            "👇 Kanalingizdan istalgan <b>bitta xabarni</b> shu yerga forward qiling.\n\n"
            "📌 <i>Qanday qilish: kanalga kiring → xabarni bosib turing → \"Forward\" → shu chatga yuboring</i>"
        ),
        "ru": (
            "👇 <b>Перешлите</b> любое сообщение из вашего канала сюда.\n\n"
            "📌 <i>Как: зайдите в канал → зажмите сообщение → \"Переслать\" → в этот чат</i>"
        ),
        "en": (
            "👇 <b>Forward</b> any message from your channel here.\n\n"
            "📌 <i>How: open channel → long-press a message → Forward → send to this chat</i>"
        ),
    }
    await query.edit_message_text(texts.get(lang, texts["uz"]), parse_mode="HTML")
    context.user_data["jf_awaiting_channel"] = True


async def jf_receive_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive channel info — via forwarded message from channel."""
    if not context.user_data.get("jf_awaiting_channel"):
        return

    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    message = update.message

    channel_id = None
    channel_title = None

    # Method 1: Forwarded message from a channel (works for private channels!)
    if message.forward_from_chat:
        channel_id = message.forward_from_chat.id
        channel_title = message.forward_from_chat.title
    # Method 2: forward_origin (newer PTB/API versions)
    elif hasattr(message, 'forward_origin') and message.forward_origin:
        origin = message.forward_origin
        if hasattr(origin, 'chat') and origin.chat:
            channel_id = origin.chat.id
            channel_title = origin.chat.title
        elif hasattr(origin, 'sender_chat') and origin.sender_chat:
            channel_id = origin.sender_chat.id
            channel_title = origin.sender_chat.title
    # Method 3: Text — @username or numeric ID (fallback)
    elif message.text:
        text = message.text.strip()
        if text.startswith("@"):
            try:
                chat = await context.bot.get_chat(text)
                channel_id = chat.id
                channel_title = chat.title
            except Exception:
                pass
        elif text.lstrip("-").isdigit():
            try:
                chat = await context.bot.get_chat(int(text))
                channel_id = chat.id
                channel_title = chat.title
            except Exception:
                pass

    if not channel_id:
        await message.reply_text(
            "❌ Kanal aniqlanmadi.\n\n"
            "📌 Kanaldan bitta xabarni shu yerga <b>forward</b> qiling." if lang == "uz"
            else "❌ Канал не определён.\n\n"
            "📌 <b>Перешлите</b> сообщение из канала сюда." if lang == "ru"
            else "❌ Channel not detected.\n\n"
            "📌 <b>Forward</b> a message from your channel here.",
            parse_mode="HTML",
        )
        return

    # Verify bot is admin in the channel
    try:
        bot_member = await context.bot.get_chat_member(channel_id, context.bot.id)
        if bot_member.status not in ("administrator", "creator"):
            await message.reply_text(
                "❌ Bot bu kanalda admin emas!\n\nAvval botni kanalga admin sifatida qo'shing." if lang == "uz"
                else "❌ Бот не админ в этом канале!\n\nСначала добавьте бота как админа." if lang == "ru"
                else "❌ Bot is not admin in this channel!\n\nAdd the bot as admin first."
            )
            return
    except Exception:
        await message.reply_text(
            "❌ Bot bu kanalga kira olmadi. Admin sifatida qo'shilganini tekshiring." if lang == "uz"
            else "❌ Бот не может получить доступ. Убедитесь, что бот — админ." if lang == "ru"
            else "❌ Bot can't access this channel. Make sure it's an admin."
        )
        return

    # Store channel info
    context.user_data["jf_awaiting_channel"] = False
    context.user_data["jf_channel_id"] = channel_id
    context.user_data["jf_channel_title"] = channel_title

    # Show mode selection buttons
    texts = {
        "uz": f"✅ <b>{channel_title}</b> topildi!\n\nRejimni tanlang:",
        "ru": f"✅ <b>{channel_title}</b> найден!\n\nВыберите режим:",
        "en": f"✅ <b>{channel_title}</b> found!\n\nChoose mode:",
    }

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Hammani qabul qilish", callback_data="jf_mode_all")],
        [InlineKeyboardButton("👩 Faqat ayollar", callback_data="jf_mode_females"),
         InlineKeyboardButton("👨 Faqat erkaklar", callback_data="jf_mode_males")],
        [InlineKeyboardButton("⭐ Faqat Premium", callback_data="jf_mode_premium")],
        [InlineKeyboardButton("❌ O'chirish", callback_data="jf_mode_off")],
    ])

    await message.reply_text(texts.get(lang, texts["uz"]), reply_markup=keyboard, parse_mode="HTML")


async def jf_mode_selected_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User selected a mode — save the join filter config."""
    query = update.callback_query
    await query.answer()

    mode = query.data.replace("jf_mode_", "")  # "all", "females", "males", "premium", "off"
    channel_id = context.user_data.get("jf_channel_id")
    channel_title = context.user_data.get("jf_channel_title", "")

    if not channel_id:
        await query.edit_message_text("❌ Xatolik. Qaytadan urinib ko'ring: /start")
        return

    # Save to database
    from bot.handlers.join_request import JoinFilter
    from bot.models.database import async_session as db_session
    from sqlalchemy import select as sql_select

    async with db_session() as session:
        result = await session.execute(
            sql_select(JoinFilter).where(JoinFilter.chat_id == channel_id)
        )
        config = result.scalar_one_or_none()

        if config:
            config.filter_mode = mode
            config.enabled = (mode != "off")
            config.chat_title = channel_title
        else:
            config = JoinFilter(
                chat_id=channel_id,
                chat_title=channel_title,
                filter_mode=mode,
                enabled=(mode != "off"),
            )
            session.add(config)
        await session.commit()

    # Clean up user_data
    context.user_data.pop("jf_channel_id", None)
    context.user_data.pop("jf_channel_title", None)

    mode_labels = {
        "all": "✅ Hammani qabul qilish",
        "females": "👩 Faqat ayollar",
        "males": "👨 Faqat erkaklar",
        "premium": "⭐ Faqat Premium",
        "off": "❌ O'chirilgan",
    }

    await query.edit_message_text(
        f"✅ <b>Tayyor!</b>\n\n"
        f"📢 Kanal: <b>{channel_title}</b>\n"
        f"📋 Rejim: {mode_labels.get(mode, mode)}\n\n"
        f"Bot endi barcha qo'shilish so'rovlarini avtomatik ko'rib chiqadi.",
        parse_mode="HTML",
    )


# ─── CAPTCHA answer handler ──────────────────────────────────────────────────


async def handle_captcha_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle CAPTCHA answer from /start flow (standalone, no ConversationHandler).
    
    IMPORTANT: This handler only processes messages when user_data has 
    'awaiting_captcha' = True. Otherwise it does NOTHING (passes through).
    """
    # Join filter: check if awaiting channel (forwarded message or text)
    if context.user_data.get("jf_awaiting_channel"):
        await jf_receive_channel(update, context)
        return

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
        # Join filter setup callbacks
        CallbackQueryHandler(jf_setup_start_callback, pattern=r"^jf_setup_start$"),
        CallbackQueryHandler(jf_mode_selected_callback, pattern=r"^jf_mode_"),
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
    """Return the CAPTCHA answer handler separately — must be added in a LATER group.
    
    Catches TEXT messages (for captcha answers) AND forwarded messages
    (for join filter channel identification via forward).
    """
    return MessageHandler(
        (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND,
        handle_captcha_answer
    )
