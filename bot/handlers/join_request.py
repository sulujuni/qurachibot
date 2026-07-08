"""Auto-accept/reject join requests for private channels/groups.

The bot must be an admin with 'can_invite_users' permission in the channel.
Admins configure filters per chat via /joinfilter command.

Filters:
  - all: accept everyone (auto-approve all requests)
  - no_bots: reject bot accounts (is_bot flag)
  - females: only accept users with typical female names
  - males: only accept users with typical male names
  - subscribed: only accept if user is subscribed to specified channels
  - started: only accept if user has started the bot (exists in user_settings)
"""

import logging
from datetime import datetime

from sqlalchemy import select, BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from telegram import ChatJoinRequest, Update
from telegram.ext import ChatJoinRequestHandler, CommandHandler, ContextTypes

from bot.config import settings
from bot.i18n import get_text
from bot.models.base import Base
from bot.models.database import async_session
from bot.models.user_settings import UserSettings
from bot.utils.lang import get_user_lang
from bot.utils.subscription import get_unsubscribed, parse_channels

logger = logging.getLogger(__name__)


# ─── Common Uzbek/Central Asian names for gender heuristic ──────────────────────

# These lists cover common Uzbek, Russian, and general Turkic names.
# Not perfect, but a reasonable heuristic for the CIS Telegram audience.
FEMALE_ENDINGS = ("a", "ya", "iya", "ova", "eva", "na", "ra", "ira", "ina", "ko")
MALE_ENDINGS = ("ov", "ev", "ich", "iy", "on", "an", "ur", "ir", "od", "id", "jon", "bek", "boy")

# Explicit common names (override endings)
FEMALE_NAMES = {
    "madina", "malika", "gulnora", "nilufar", "dildora", "zulfiya", "feruza",
    "shoira", "sevinch", "barno", "muazzam", "mohira", "dilnoza", "nodira",
    "nargiza", "ozoda", "umida", "iroda", "yulduz", "shahlo", "dilorom",
    "fotima", "maftuna", "nafisa", "xurshida", "rano", "gulbahor", "munira",
    "anna", "maria", "elena", "olga", "natasha", "svetlana", "irina", "yana",
    "diana", "alina", "daria", "sofia", "anastasia", "ekaterina", "polina",
}

MALE_NAMES = {
    "muhammad", "abdulloh", "islom", "sardor", "jasur", "sherzod", "bobur",
    "ulugbek", "nodir", "firdavs", "otabek", "asilbek", "bekzod", "jahongir",
    "dilshod", "rustam", "timur", "alisher", "sanjar", "anvar", "behruz",
    "doniyor", "eldor", "farkhod", "mirzo", "suhrab", "tohir", "zafarjon",
    "ivan", "aleksandr", "dmitriy", "sergey", "andrey", "nikita", "maxim",
    "pavel", "artem", "igor", "oleg", "roman", "kirill", "denis", "anton",
}


def _guess_gender(first_name: str | None) -> str | None:
    """Guess gender from first name. Returns 'male', 'female', or None (unknown)."""
    if not first_name:
        return None

    name = first_name.lower().strip().split()[0]  # first word only

    # Check explicit name lists first
    if name in FEMALE_NAMES:
        return "female"
    if name in MALE_NAMES:
        return "male"

    # Heuristic by ending
    for ending in sorted(FEMALE_ENDINGS, key=len, reverse=True):
        if name.endswith(ending) and len(name) > len(ending) + 1:
            return "female"
    for ending in sorted(MALE_ENDINGS, key=len, reverse=True):
        if name.endswith(ending) and len(name) > len(ending) + 1:
            return "male"

    return None


# ─── Database model for per-chat join filter config ──────────────────────────────


class JoinFilter(Base):
    """Per-chat configuration for join request auto-processing."""

    __tablename__ = "join_filters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    chat_title: Mapped[str] = mapped_column(String(255), nullable=True)
    filter_mode: Mapped[str] = mapped_column(String(50), default="all", nullable=False)
    # Comma-separated channels for 'subscribed' mode
    required_channels: Mapped[str] = mapped_column(Text, nullable=True)
    # Stats
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def __repr__(self) -> str:
        return f"<JoinFilter(chat_id={self.chat_id}, mode={self.filter_mode})>"


# ─── Join Request Handler ────────────────────────────────────────────────────────


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process incoming join requests based on configured filters."""
    request: ChatJoinRequest = update.chat_join_request
    if not request:
        return

    chat_id = request.chat.id
    user = request.from_user

    # Look up filter config for this chat
    async with async_session() as session:
        result = await session.execute(
            select(JoinFilter).where(JoinFilter.chat_id == chat_id)
        )
        config = result.scalar_one_or_none()

    if not config or not config.enabled:
        return  # No filter configured — let the request sit for manual review

    mode = config.filter_mode
    accepted = False
    reason = ""

    # ALL modes (except 'off' and 'premium') require CAPTCHA verification first.
    # This eliminates bots without needing a separate 'no_bots' mode.
    if mode not in ("off", "premium"):
        if user.is_bot:
            accepted = False
            reason = "Bot account"
            # Skip to decision
            try:
                if accepted:
                    await request.approve()
                else:
                    await request.decline()
            except Exception as e:
                logger.error("Failed to process join request for user %s: %s", user.id, e)
            return

        # Check CAPTCHA verification
        async with async_session() as session:
            result = await session.execute(
                select(UserSettings).where(UserSettings.user_id == user.id)
            )
            user_settings = result.scalar_one_or_none()

        is_verified = user_settings.captcha_verified if user_settings else False

        if not is_verified:
            # Not verified — decline and DM instructions
            try:
                await request.decline()
            except Exception:
                pass
            try:
                bot_username = context.bot.username
                await context.bot.send_message(
                    user.id,
                    f"👋 Guruhga qo'shilish uchun avval tekshiruvdan o'ting:\n"
                    f"https://t.me/{bot_username}?start=verify\n\n"
                    f"CAPTCHA ni yeching, keyin qayta so'rov yuboring.",
                )
            except Exception:
                pass
            # Update stats
            async with async_session() as session:
                result = await session.execute(select(JoinFilter).where(JoinFilter.chat_id == chat_id))
                cfg = result.scalar_one_or_none()
                if cfg:
                    cfg.rejected = (cfg.rejected or 0) + 1
                    await session.commit()
            return

    # Now apply mode-specific filter (user is already CAPTCHA-verified at this point)
    if mode == "all":
        accepted = True

    elif mode == "females":
        gender = user_settings.gender if user_settings else None
        if gender == "female":
            accepted = True
        elif gender == "male":
            accepted = False
            reason = "Only female users accepted"
        else:
            # Gender not set — fallback to name heuristic
            guessed = _guess_gender(user.first_name)
            accepted = (guessed == "female")
            if not accepted:
                reason = "Only female users accepted"

    elif mode == "males":
        gender = user_settings.gender if user_settings else None
        if gender == "male":
            accepted = True
        elif gender == "female":
            accepted = False
            reason = "Only male users accepted"
        else:
            guessed = _guess_gender(user.first_name)
            accepted = (guessed == "male")
            if not accepted:
                reason = "Only male users accepted"

    elif mode == "subscribed":
        channels = parse_channels(config.required_channels)
        if channels:
            missing = await get_unsubscribed(context.bot, user.id, channels)
            if missing:
                accepted = False
                reason = f"Must subscribe to: {', '.join(missing)}"
            else:
                accepted = True
        else:
            accepted = True

    elif mode == "premium":
        # Premium doesn't require CAPTCHA — it's its own strong verification
        if user.is_bot:
            accepted = False
            reason = "Bot"
        else:
            is_premium = getattr(user, "is_premium", False)
            accepted = bool(is_premium)
            if not accepted:
                reason = "Premium subscription required"

    elif mode == "off":
        return  # Do nothing

    else:
        return

    # Execute decision
    try:
        if accepted:
            await request.approve()
            logger.info("Approved join request: user %s in chat %s (mode: %s)", user.id, chat_id, mode)
        else:
            await request.decline()
            logger.info("Declined join request: user %s in chat %s (mode: %s, reason: %s)", user.id, chat_id, mode, reason)
    except Exception as e:
        logger.error("Failed to process join request for user %s: %s", user.id, e)
        return

    # Update stats
    async with async_session() as session:
        result = await session.execute(
            select(JoinFilter).where(JoinFilter.chat_id == chat_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg:
            if accepted:
                cfg.accepted = (cfg.accepted or 0) + 1
            else:
                cfg.rejected = (cfg.rejected or 0) + 1
            await session.commit()


# ─── Configuration Command ───────────────────────────────────────────────────────


async def joinfilter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Configure join request filter. Command: /joinfilter <mode> [channels]

    Modes:
      all            — Accept everyone (who passed CAPTCHA)
      females        — Only accept female users
      males          — Only accept male users
      subscribed     — Must be subscribed to channels (provide channels after mode)
      premium        — Only Telegram Premium users
      off            — Disable filtering (manual review)

    All modes (except premium/off) automatically require CAPTCHA verification.
    Bots and unverified users are declined automatically.

    Examples:
      /joinfilter all
      /joinfilter females
      /joinfilter subscribed @channel1, @channel2
      /joinfilter off
    """
    user_id = update.effective_user.id
    chat = update.effective_chat

    # Must be used in a group/channel or by admin in private
    if chat.type == "private":
        # Check if admin
        from bot.handlers.admin import is_admin as check_admin
        if not check_admin(user_id):
            await update.message.reply_text(
                "Bu buyruqni guruh/kanalda ishlating (bot admin bo'lishi kerak)."
            )
            return

    # In a group/channel — check if user is an admin of that chat
    if chat.type in ("group", "supergroup", "channel"):
        try:
            member = await context.bot.get_chat_member(chat.id, user_id)
            if member.status not in ("creator", "administrator"):
                await update.message.reply_text("⛔ Faqat admin bu buyruqni ishlata oladi.")
                return
        except Exception:
            pass

    if not context.args:
        # Show current config
        async with async_session() as session:
            result = await session.execute(
                select(JoinFilter).where(JoinFilter.chat_id == chat.id)
            )
            config = result.scalar_one_or_none()

        if not config:
            await update.message.reply_text(
                "📋 <b>Join Request Filter</b>\n\n"
                "Hozircha sozlanmagan. Sozlash:\n\n"
                "⚠️ Barcha rejimlar avtomatik CAPTCHA talab qiladi (botlar o'tolmaydi).\n\n"
                "<code>/joinfilter all</code> — hammani qabul qilish\n"
                "<code>/joinfilter females</code> — faqat ayollar\n"
                "<code>/joinfilter males</code> — faqat erkaklar\n"
                "<code>/joinfilter subscribed @ch1,@ch2</code> — obuna bo'lganlar\n"
                "<code>/joinfilter premium</code> — ⭐ faqat Premium\n"
                "<code>/joinfilter off</code> — o'chirish",
                parse_mode="HTML",
            )
        else:
            status = "✅ Faol" if config.enabled else "❌ O'chirilgan"
            channels = config.required_channels or "—"
            await update.message.reply_text(
                f"📋 <b>Join Request Filter</b>\n\n"
                f"Chat: <b>{config.chat_title or chat.title or chat.id}</b>\n"
                f"Rejim: <b>{config.filter_mode}</b>\n"
                f"Holat: {status}\n"
                f"Kanallar: {channels}\n\n"
                f"📊 Qabul: {config.accepted} | Rad: {config.rejected}\n\n"
                f"O'zgartirish: <code>/joinfilter &lt;mode&gt;</code>",
                parse_mode="HTML",
            )
        return

    mode = context.args[0].lower()
    valid_modes = ("all", "females", "males", "subscribed", "premium", "off")

    if mode not in valid_modes:
        await update.message.reply_text(
            f"❌ Noto'g'ri rejim. Mavjud rejimlar:\n"
            f"{', '.join(valid_modes)}"
        )
        return

    # Parse channels for 'subscribed' mode
    channels_str = None
    if mode == "subscribed" and len(context.args) > 1:
        from bot.utils.subscription import serialize_channels
        raw = " ".join(context.args[1:])
        channels_str = serialize_channels(parse_channels(raw))

    # Save config
    async with async_session() as session:
        result = await session.execute(
            select(JoinFilter).where(JoinFilter.chat_id == chat.id)
        )
        config = result.scalar_one_or_none()

        if config:
            config.filter_mode = mode
            config.enabled = (mode != "off")
            config.chat_title = chat.title
            if channels_str:
                config.required_channels = channels_str
        else:
            config = JoinFilter(
                chat_id=chat.id,
                chat_title=chat.title,
                filter_mode=mode,
                enabled=(mode != "off"),
                required_channels=channels_str,
            )
            session.add(config)

        await session.commit()

    mode_labels = {
        "all": "✅ Hammani qabul qilish (CAPTCHA o'tganlar)",
        "females": "👩 Faqat ayollar",
        "males": "👨 Faqat erkaklar",
        "subscribed": f"📢 Obuna bo'lganlar ({channels_str or 'kanallar kerak'})",
        "premium": "⭐ Faqat Premium foydalanuvchilar",
        "off": "❌ O'chirilgan (qo'lda ko'rib chiqish)",
    }

    await update.message.reply_text(
        f"✅ <b>Join filter sozlandi!</b>\n\n"
        f"Rejim: {mode_labels[mode]}\n\n"
        f"Endi yangi so'rovlar avtomatik ko'rib chiqiladi.",
        parse_mode="HTML",
    )


# ─── Handler Registration ────────────────────────────────────────────────────────


def get_join_request_handlers() -> list:
    """Return join request handlers."""
    return [
        ChatJoinRequestHandler(handle_join_request),
        CommandHandler("joinfilter", joinfilter_command),
    ]
