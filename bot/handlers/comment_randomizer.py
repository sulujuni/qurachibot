"""Comment Randomizer — quick tool for picking random/specific comments.

This is the simplest "in comment" konkurs tool:
1. Admin posts something in a channel (manually or via bot)
2. People comment on it
3. Admin uses /pickrandom or /pickcomment to choose a winner
4. Bot announces the result

Unlike group_giveaway (which tracks entries live), this is a one-shot
tool that reads existing comments at draw time. Perfect for quick
mini-giveaways in channel discussions.
"""

import logging
import random

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler

from bot.i18n import get_text
from bot.utils.lang import get_user_lang

logger = logging.getLogger(__name__)


async def _get_discussion_comments(bot, chat_id: int, message_id: int, limit: int = 500) -> list:
    """Fetch replies/comments to a specific message in a group/channel discussion.

    Returns a list of comment dicts: {user_id, username, first_name, text, message_id}
    """
    # Note: Telegram Bot API doesn't have a "get replies to message" endpoint.
    # The workaround: we use getChat + recent messages. But for channels with
    # linked discussion groups, comments are replies to the forwarded post.
    #
    # The practical approach: the admin forwards the post to the bot (or uses
    # the command in the discussion group as a reply to the target post), and
    # we collect users from the reply thread using get_chat history.
    #
    # Since Bot API has no getChatHistory, we track comments via our existing
    # GroupGiveawayEntry table or collect them at command time from context.
    return []


async def pickrandom_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick random comment(s) from a channel/group post.

    Usage (reply to the giveaway post in discussion group):
      /pickrandom        — pick 1 random commenter
      /pickrandom 3      — pick 3 random commenters

    The bot collects all unique users who replied to that post and picks randomly.
    """
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    message = update.message

    # Must be a reply to a post
    if not message.reply_to_message:
        await message.reply_text(
            "💡 <b>Foydalanish:</b>\n\n"
            "1. Konkurs postiga javob (reply) qilib ushbu buyruqni yuboring\n"
            "2. Bot izoh qoldirgan barcha foydalanuvchilardan tasodifiy tanlaydi\n\n"
            "<code>/pickrandom</code> — 1 ta tasodifiy g'olib\n"
            "<code>/pickrandom 3</code> — 3 ta tasodifiy g'olib\n\n"
            "⚠️ Bu buyruqni guruh/kanaldagi muhokama guruhida ishlating, "
            "konkurs postiga reply qilib.",
            parse_mode="HTML",
        )
        return

    # How many winners?
    count = 1
    if context.args:
        try:
            count = max(1, int(context.args[0]))
        except ValueError:
            pass

    replied_msg = message.reply_to_message
    chat_id = message.chat_id

    # Collect unique commenters from the thread
    # We'll use our GroupGiveawayEntry table if this post is being tracked,
    # otherwise inform the user to use the tracked approach.

    from bot.models.database import async_session
    from bot.models.group_giveaway import GroupGiveaway, GroupGiveawayEntry, GroupGiveawayStatus
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from bot.handlers.group_giveaway import _resolve_giveaway_id

    # Check if this post is a tracked giveaway
    giveaway_id = await _resolve_giveaway_id(chat_id, replied_msg.message_id)

    if giveaway_id:
        # Use tracked entries
        async with async_session() as session:
            result = await session.execute(
                select(GroupGiveawayEntry).where(
                    GroupGiveawayEntry.giveaway_id == giveaway_id,
                    GroupGiveawayEntry.is_valid == True,
                )
            )
            entries = result.scalars().all()

        if not entries:
            await message.reply_text("❌ Bu postga hali hech kim izoh qoldirmagan!")
            return

        # Pick random
        winner_count = min(count, len(entries))
        winners = random.sample(entries, winner_count)

        winners_text = "\n".join(
            f"🏆 {i+1}. {'@' + w.username if w.username else w.first_name or f'ID:{w.user_id}'}"
            f" — \"{(w.comment_text or '')[:50]}\""
            for i, w in enumerate(winners)
        )

        await message.reply_text(
            f"🎲 <b>TASODIFIY TANLASH NATIJALARI</b>\n\n"
            f"📋 Jami izohlar: {len(entries)}\n"
            f"🏆 Tanlangan: {winner_count}\n\n"
            f"<b>G'olib(lar):</b>\n{winners_text}\n\n"
            f"Tabriklaymiz! 🎉",
            parse_mode="HTML",
        )
    else:
        # Not a tracked giveaway — tell user how to use it
        await message.reply_text(
            "ℹ️ Bu post hali bot tomonidan kuzatilmayapti.\n\n"
            "<b>2 ta variant:</b>\n\n"
            "1️⃣ <b>Kuzatiladigan o'yin yaratish:</b>\n"
            "   /groupgiveaway yoki /channelgiveaway bilan yarating — "
            "bot barcha izohlarni avtomatik saqlaydi\n\n"
            "2️⃣ <b>Tez randomizatsiya:</b>\n"
            "   Ushbu postga /trackpost buyrug'ini reply qilib yuboring — "
            "bot shu paytdan boshlab izohlarni yig'adi\n\n"
            "Keyin /pickrandom bilan g'olib tanlaysiz.",
            parse_mode="HTML",
        )


async def pickcomment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick a specific comment by number.

    Usage (reply to the giveaway post):
      /pickcomment 5    — pick the 5th commenter as winner
    """
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    message = update.message

    if not message.reply_to_message:
        await message.reply_text(
            "💡 Konkurs postiga reply qilib <code>/pickcomment &lt;raqam&gt;</code> yuboring.\n"
            "Masalan: <code>/pickcomment 5</code> — 5-chi izohchini g'olib qiladi.",
            parse_mode="HTML",
        )
        return

    if not context.args:
        await message.reply_text("❌ Raqam kiriting. Masalan: <code>/pickcomment 3</code>", parse_mode="HTML")
        return

    try:
        pick_num = int(context.args[0])
    except ValueError:
        await message.reply_text("❌ Noto'g'ri raqam.")
        return

    replied_msg = message.reply_to_message
    chat_id = message.chat_id

    from bot.models.database import async_session
    from bot.models.group_giveaway import GroupGiveawayEntry
    from sqlalchemy import select
    from bot.handlers.group_giveaway import _resolve_giveaway_id

    giveaway_id = await _resolve_giveaway_id(chat_id, replied_msg.message_id)

    if not giveaway_id:
        await message.reply_text(
            "ℹ️ Bu post kuzatilmayapti. Avval /groupgiveaway bilan o'yin yarating "
            "yoki /trackpost bilan kuzatishni boshlang.",
            parse_mode="HTML",
        )
        return

    async with async_session() as session:
        result = await session.execute(
            select(GroupGiveawayEntry).where(
                GroupGiveawayEntry.giveaway_id == giveaway_id,
                GroupGiveawayEntry.is_valid == True,
            ).order_by(GroupGiveawayEntry.entered_at)
        )
        entries = result.scalars().all()

    if not entries:
        await message.reply_text("❌ Hali izohlar yo'q!")
        return

    if pick_num < 1 or pick_num > len(entries):
        await message.reply_text(f"❌ Oraliq: 1 — {len(entries)}")
        return

    winner = entries[pick_num - 1]
    name = f"@{winner.username}" if winner.username else (winner.first_name or f"ID:{winner.user_id}")

    await message.reply_text(
        f"🎯 <b>#{pick_num}-CHI IZOHCHI G'OLIB!</b>\n\n"
        f"🏆 {name}\n"
        f"💬 \"{(winner.comment_text or '')[:100]}\"\n\n"
        f"Tabriklaymiz! 🎉",
        parse_mode="HTML",
    )


async def trackpost_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start tracking comments on an existing post for later randomization.

    Usage: Reply to any post with /trackpost
    The bot will start recording comments on that post.
    Then use /pickrandom or /pickcomment to draw winners.
    """
    user_id = update.effective_user.id
    message = update.message

    if not message.reply_to_message:
        await message.reply_text(
            "💡 Istalgan postga reply qilib <code>/trackpost</code> yuboring.\n"
            "Bot shu paytdan boshlab izohlarni yig'a boshlaydi.\n"
            "Keyin /pickrandom yoki /pickcomment bilan g'olib tanlaysiz.",
            parse_mode="HTML",
        )
        return

    replied_msg = message.reply_to_message
    chat_id = message.chat_id

    from bot.models.database import async_session
    from bot.models.group_giveaway import GroupGiveaway, GroupGiveawayMode, GroupGiveawayStatus
    from sqlalchemy import select
    from bot.handlers.group_giveaway import _active_giveaway_posts, _resolve_giveaway_id

    # Check if already tracked
    existing_id = await _resolve_giveaway_id(chat_id, replied_msg.message_id)
    if existing_id:
        await message.reply_text("✅ Bu post allaqachon kuzatilmoqda! /pickrandom bilan g'olib tanlang.")
        return

    # Create a lightweight tracking entry (group giveaway with no end time)
    async with async_session() as session:
        gw = GroupGiveaway(
            title=f"Izoh randomizer #{replied_msg.message_id}",
            prize="—",
            winner_count=1,
            mode=GroupGiveawayMode.RANDOM,
            creator_id=user_id,
            creator_username=message.from_user.username,
            chat_id=chat_id,
            message_id=replied_msg.message_id,
            is_channel_post=False,
        )
        session.add(gw)
        await session.commit()
        await session.refresh(gw)

    # Register in memory for live tracking
    _active_giveaway_posts[(chat_id, replied_msg.message_id)] = gw.id

    await message.reply_text(
        f"✅ <b>Kuzatish boshlandi!</b>\n\n"
        f"Endi shu postga izoh qoldirgan har bir kishi avtomatik qayd etiladi.\n\n"
        f"G'olib tanlash:\n"
        f"• <code>/pickrandom</code> — tasodifiy\n"
        f"• <code>/pickrandom 3</code> — 3 ta tasodifiy\n"
        f"• <code>/pickcomment 5</code> — 5-chi izohchi\n\n"
        f"Bu postga reply qilib buyruqni yuboring.",
        parse_mode="HTML",
    )


# ─── Handler Registration ────────────────────────────────────────────────────────


def get_comment_randomizer_handlers() -> list:
    """Return comment randomizer handlers."""
    return [
        CommandHandler("pickrandom", pickrandom_command),
        CommandHandler("pickcomment", pickcomment_command),
        CommandHandler("trackpost", trackpost_command),
    ]
