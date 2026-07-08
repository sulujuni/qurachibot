"""CAPTCHA verification handler.

Every user must pass a simple math CAPTCHA before they can:
- Join giveaways
- Enter contests
- Get approved via join request filters (mode: started/strict/verified)

Flow:
1. User starts bot → if not verified, bot sends CAPTCHA challenge
2. User answers correctly → marked as captcha_verified=True
3. All participation checks verify captcha_verified before proceeding
"""

import logging

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.models.database import async_session
from bot.models.user_settings import UserSettings
from bot.utils.captcha import generate_captcha, verify_captcha

logger = logging.getLogger(__name__)

# Conversation states
AWAITING_ANSWER, AWAITING_GENDER = range(2)


async def is_user_verified(user_id: int) -> bool:
    """Check if a user has passed the CAPTCHA verification."""
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings.captcha_verified).where(UserSettings.user_id == user_id)
        )
        verified = result.scalar_one_or_none()
    return bool(verified)


async def mark_verified(user_id: int, gender: str = None) -> None:
    """Mark a user as CAPTCHA-verified and optionally store gender."""
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        settings = result.scalar_one_or_none()
        if settings:
            settings.captcha_verified = True
            if gender:
                settings.gender = gender
        else:
            settings = UserSettings(user_id=user_id, language="uz", captcha_verified=True, gender=gender)
            session.add(settings)
        await session.commit()


async def send_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send a CAPTCHA challenge to the user. Command: /verify or auto on /start."""
    user_id = update.effective_user.id

    # Check if already verified
    if await is_user_verified(user_id):
        await update.message.reply_text(
            "✅ Siz allaqachon tasdiqlangansiz! Barcha funksiyalar sizga ochiq.",
        )
        return ConversationHandler.END

    # Generate captcha
    captcha = generate_captcha()
    context.user_data["captcha_answer"] = captcha.answer
    context.user_data["captcha_attempts"] = 0

    await update.message.reply_text(
        f"🔒 <b>Tekshiruv (CAPTCHA)</b>\n\n"
        f"Siz haqiqiy odamsiz ekanligingizni tasdiqlang.\n"
        f"Quyidagi misolni yeching:\n\n"
        f"🧮 <code>{captcha.question}</code>\n\n"
        f"Javobni raqam bilan yuboring:",
        parse_mode="HTML",
    )
    return AWAITING_ANSWER


async def check_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Check the user's CAPTCHA answer."""
    user_id = update.effective_user.id
    user_answer = update.message.text.strip()
    correct_answer = context.user_data.get("captcha_answer")
    attempts = context.user_data.get("captcha_attempts", 0)

    if correct_answer is None:
        # No active captcha — restart
        return await send_captcha(update, context)

    if verify_captcha(user_answer, correct_answer):
        # Correct! Now ask gender
        context.user_data.pop("captcha_answer", None)
        context.user_data.pop("captcha_attempts", None)

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
        return AWAITING_GENDER
    else:
        # Wrong answer
        attempts += 1
        context.user_data["captcha_attempts"] = attempts

        if attempts >= 3:
            # Too many attempts — generate new captcha
            captcha = generate_captcha()
            context.user_data["captcha_answer"] = captcha.answer
            context.user_data["captcha_attempts"] = 0

            await update.message.reply_text(
                f"❌ Noto'g'ri javob (3 ta urinish tugadi).\n\n"
                f"Yangi misol:\n"
                f"🧮 <code>{captcha.question}</code>\n\n"
                f"Javobni raqam bilan yuboring:",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"❌ Noto'g'ri. Yana urinib ko'ring ({3 - attempts} urinish qoldi).\n"
                f"Javobni raqam bilan yuboring:",
            )
        return AWAITING_ANSWER


async def cancel_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel captcha verification."""
    context.user_data.pop("captcha_answer", None)
    context.user_data.pop("captcha_attempts", None)
    await update.message.reply_text(
        "⚠️ Tekshiruv bekor qilindi.\n"
        "O'yinlarda qatnashish uchun /verify buyrug'i bilan tasdiqlaning.",
    )
    return ConversationHandler.END


async def gender_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle gender selection after CAPTCHA."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    gender = query.data.split("_")[1]  # "male" or "female"
    await mark_verified(user_id, gender=gender)

    gender_emoji = "👨" if gender == "male" else "👩"
    await query.edit_message_text(
        f"✅ <b>Tasdiqlandi!</b> {gender_emoji}\n\n"
        "Siz haqiqiy foydalanuvchi ekansiz. Endi barcha funksiyalar sizga ochiq:\n"
        "• Yutuqli o'yinlarda qatnashish\n"
        "• Konkurslarda ishtirok etish\n"
        "• Kanallarga qo'shilish\n\n"
        "Davom etish uchun /help ni bosing.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ─── Helper for other handlers to check verification ─────────────────────────


async def require_verification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is verified. If not, prompt them to verify.

    Returns True if verified, False if not (and sends a message).
    Use this in giveaway/contest handlers before allowing participation.
    """
    user_id = update.effective_user.id
    if await is_user_verified(user_id):
        return True

    # Not verified — send prompt
    bot_username = context.bot.username
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 Tekshiruvdan o'tish", url=f"https://t.me/{bot_username}?start=verify")]
    ])

    if update.callback_query:
        await update.callback_query.answer(
            "🔒 Avval tekshiruvdan o'ting! Bot bilan shaxsiy chatga o'ting.",
            show_alert=True,
        )
    elif update.message:
        await update.message.reply_text(
            "🔒 <b>Tekshiruv talab qilinadi</b>\n\n"
            "Qatnashish uchun avval oddiy matematik misolni yeching.\n"
            "Bu botlardan himoya qilish uchun kerak.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    return False


# ─── Handler Registration ────────────────────────────────────────────────────────


def get_captcha_handlers() -> list:
    """Return captcha verification handlers."""
    captcha_conv = ConversationHandler(
        entry_points=[CommandHandler("verify", send_captcha)],
        states={
            AWAITING_ANSWER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_answer),
            ],
            AWAITING_GENDER: [
                CallbackQueryHandler(gender_selected, pattern=r"^gender_(male|female)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_captcha)],
    )

    return [captcha_conv]
