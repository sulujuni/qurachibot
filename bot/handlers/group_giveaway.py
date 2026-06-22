"""Group & Channel comment-based giveaway handlers.

Supports:
- Groups: Users reply to a giveaway post to enter
- Channels: Admin posts giveaway, users comment in discussion to enter

Bot picks winners from commenters.
"""

import random
from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.i18n import get_text
from bot.models import (
    GroupGiveaway, GroupGiveawayEntry, GroupGiveawayMode,
    GroupGiveawayStatus, GroupGiveawayWinner, async_session,
)
from bot.utils.lang import get_user_lang
from bot.utils.moderation import is_blacklisted
from bot.utils.rate_limit import check_rate_limit, log_action
from bot.utils.loyalty import award_points


# Conversation states for group giveaway creation
GG_TITLE, GG_PRIZE, GG_MODE, GG_KEYWORD, GG_WINNERS, GG_DURATION = range(6)

# Store active group giveaways:
# Key: (chat_id, message_id) → giveaway_id
# For channels: also map (discussion_group_id, forwarded_msg_id) → giveaway_id
_active_giveaway_posts: dict[tuple[int, int], int] = {}

# Map channel_post_id → giveaway_id (for channel comment tracking)
_channel_post_giveaways: dict[tuple[int, int], int] = {}



async def _load_active_giveaways():
    """Load active group/channel giveaways into memory on startup."""
    async with async_session() as session:
        result = await session.execute(
            select(GroupGiveaway).where(
                GroupGiveaway.status == GroupGiveawayStatus.ACTIVE,
                GroupGiveaway.message_id != None,
            )
        )
        for gw in result.scalars().all():
            _active_giveaway_posts[(gw.chat_id, gw.message_id)] = gw.id
            # If it's a channel post, also track by channel_id + message_id
            if gw.is_channel_post:
                _channel_post_giveaways[(gw.chat_id, gw.message_id)] = gw.id



# ─── Create Giveaway (works in groups AND channels) ─────────────────────────────


async def groupgiveaway_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start group/channel giveaway creation. Command: /groupgiveaway"""
    chat = update.effective_chat
    chat_type = chat.type

    # Allow: groups, supergroups, and channels
    if chat_type == "private":
        await update.message.reply_text(
            "❌ This command works in groups and channels!\n\n"
            "• <b>In a group:</b> Use /groupgiveaway directly\n"
            "• <b>For a channel:</b> Use /channelgiveaway in the "
            "channel's linked discussion group, or make the bot "
            "an admin in your channel and use /channelgiveaway there.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    context.user_data["gg_lang"] = lang
    context.user_data["gg_chat_id"] = chat.id
    context.user_data["gg_is_channel"] = (chat_type == "channel")

    source = "Channel" if chat_type == "channel" else "Group"

    await update.message.reply_text(
        f"🎯 <b>Create a {source} Comment Giveaway</b>\n\n"
        f"Users will comment on the giveaway post to enter.\n"
        f"Only ONE comment per user counts!\n\n"
        f"What's the <b>title</b> of this giveaway?\n\n"
        f"Send /cancel to abort.",
        parse_mode="HTML",
    )
    return GG_TITLE


async def channelgiveaway_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start channel giveaway (alias). Command: /channelgiveaway"""
    chat = update.effective_chat

    if chat.type == "private":
        await update.message.reply_text(
            "❌ Use this command in:\n"
            "• Your channel (bot must be admin)\n"
            "• The channel's linked discussion group\n\n"
            "The bot will post the giveaway and track "
            "comments as entries automatically.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    context.user_data["gg_lang"] = lang
    context.user_data["gg_chat_id"] = chat.id
    context.user_data["gg_is_channel"] = True

    await update.message.reply_text(
        "📢 <b>Create a Channel Giveaway</b>\n\n"
        "The giveaway will be posted in this chat.\n"
        "Users comment to enter — only FIRST comment counts!\n\n"
        "What's the <b>title</b>?\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return GG_TITLE



async def gg_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive title."""
    context.user_data["gg_title"] = update.message.text.strip()
    await update.message.reply_text(
        "🎁 What's the <b>prize</b>?",
        parse_mode="HTML",
    )
    return GG_PRIZE


async def gg_prize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive prize."""
    context.user_data["gg_prize"] = update.message.text.strip()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Random Winner", callback_data="ggmode_random")],
        [InlineKeyboardButton("⚡ First N Commenters", callback_data="ggmode_first_n")],
        [InlineKeyboardButton("🔑 Keyword Required", callback_data="ggmode_keyword")],
    ])

    await update.message.reply_text(
        "📋 <b>Choose the giveaway mode:</b>\n\n"
        "🎲 <b>Random</b> — Pick random winner(s) from all commenters\n"
        "⚡ <b>First N</b> — First N people to comment win\n"
        "🔑 <b>Keyword</b> — Must include a specific word/phrase\n",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    return GG_MODE


async def gg_mode_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle mode selection."""
    query = update.callback_query
    await query.answer()

    mode_map = {
        "ggmode_random": GroupGiveawayMode.RANDOM,
        "ggmode_first_n": GroupGiveawayMode.FIRST_N,
        "ggmode_keyword": GroupGiveawayMode.KEYWORD,
    }
    context.user_data["gg_mode"] = mode_map[query.data]

    if query.data == "ggmode_keyword":
        await query.edit_message_text(
            "🔑 What <b>keyword</b> must commenters include?\n"
            "(e.g., a specific word, hashtag, or phrase)",
            parse_mode="HTML",
        )
        return GG_KEYWORD
    else:
        context.user_data["gg_keyword"] = None
        await query.edit_message_text(
            "🏆 How many <b>winners</b>?\n(Send a number, e.g., 1, 3, 5)",
            parse_mode="HTML",
        )
        return GG_WINNERS


async def gg_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive keyword."""
    context.user_data["gg_keyword"] = update.message.text.strip()
    await update.message.reply_text(
        "🏆 How many <b>winners</b>?\n(Send a number, e.g., 1, 3, 5)",
        parse_mode="HTML",
    )
    return GG_WINNERS


async def gg_winners(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive winner count."""
    try:
        count = int(update.message.text.strip())
        if count < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid number (1 or more).")
        return GG_WINNERS

    context.user_data["gg_winner_count"] = count

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 hour", callback_data="ggdur_1h"),
            InlineKeyboardButton("6 hours", callback_data="ggdur_6h"),
        ],
        [
            InlineKeyboardButton("12 hours", callback_data="ggdur_12h"),
            InlineKeyboardButton("24 hours", callback_data="ggdur_24h"),
        ],
        [
            InlineKeyboardButton("3 days", callback_data="ggdur_3d"),
            InlineKeyboardButton("7 days", callback_data="ggdur_7d"),
        ],
        [InlineKeyboardButton("No time limit", callback_data="ggdur_none")],
    ])

    await update.message.reply_text(
        "⏱ How long should comments be accepted?",
        reply_markup=keyboard,
    )
    return GG_DURATION



async def gg_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle duration and create the giveaway post."""
    query = update.callback_query
    await query.answer()

    duration_map = {
        "ggdur_1h": timedelta(hours=1),
        "ggdur_6h": timedelta(hours=6),
        "ggdur_12h": timedelta(hours=12),
        "ggdur_24h": timedelta(hours=24),
        "ggdur_3d": timedelta(days=3),
        "ggdur_7d": timedelta(days=7),
        "ggdur_none": None,
    }
    duration = duration_map.get(query.data)
    ends_at = datetime.utcnow() + duration if duration else None

    mode = context.user_data["gg_mode"]
    keyword = context.user_data.get("gg_keyword")
    chat_id = context.user_data["gg_chat_id"]
    is_channel = context.user_data.get("gg_is_channel", False)

    # Save to database
    async with async_session() as session:
        giveaway = GroupGiveaway(
            title=context.user_data["gg_title"],
            prize=context.user_data["gg_prize"],
            winner_count=context.user_data["gg_winner_count"],
            mode=mode,
            keyword=keyword,
            creator_id=query.from_user.id,
            creator_username=query.from_user.username,
            chat_id=chat_id,
            ends_at=ends_at,
            is_channel_post=is_channel,
        )
        session.add(giveaway)
        await session.commit()
        await session.refresh(giveaway)

    # Build the announcement
    mode_labels = {
        GroupGiveawayMode.RANDOM: "🎲 Random winner from commenters",
        GroupGiveawayMode.FIRST_N: f"⚡ First {giveaway.winner_count} to comment win",
        GroupGiveawayMode.KEYWORD: f"🔑 Must include: \"{keyword}\"",
    }
    end_text = (
        f"📅 Ends: {ends_at.strftime('%Y-%m-%d %H:%M UTC')}"
        if ends_at else "📅 No time limit (manual draw)"
    )
    keyword_text = f"\n🔑 Required keyword: <b>{keyword}</b>" if keyword else ""
    source_icon = "📢" if is_channel else "🎯"

    announcement = (
        f"{source_icon} <b>GIVEAWAY: {giveaway.title}</b>\n\n"
        f"🎁 Prize: <b>{giveaway.prize}</b>\n"
        f"🏆 Winners: {giveaway.winner_count}\n"
        f"📋 Mode: {mode_labels[mode]}{keyword_text}\n"
        f"{end_text}\n\n"
        f"<b>HOW TO ENTER:</b>\n"
        f"💬 Comment on this post to participate!\n"
        f"⚠️ Only your FIRST comment counts.\n\n"
        f"👤 Entries: 0\n"
        f"ID: <code>{giveaway.id}</code>"
    )

    # Delete the conversation message
    try:
        await query.message.delete()
    except Exception:
        pass

    # Send the giveaway post
    sent_message = await context.bot.send_message(
        chat_id=chat_id,
        text=announcement,
        parse_mode="HTML",
    )

    # Store message_id
    async with async_session() as session:
        result = await session.execute(
            select(GroupGiveaway).where(GroupGiveaway.id == giveaway.id)
        )
        gw = result.scalar_one()
        gw.message_id = sent_message.message_id
        await session.commit()

    # Track in memory
    _active_giveaway_posts[(chat_id, sent_message.message_id)] = giveaway.id
    if is_channel:
        _channel_post_giveaways[(chat_id, sent_message.message_id)] = giveaway.id

    context.user_data.clear()
    return ConversationHandler.END


async def gg_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel group/channel giveaway creation."""
    context.user_data.clear()
    await update.message.reply_text("❌ Giveaway creation cancelled.")
    return ConversationHandler.END



# ─── Handle Replies/Comments (Group + Channel Discussion) ────────────────────────


async def handle_group_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process replies to giveaway posts as entries.

    Handles:
    1. Direct replies to a giveaway message in a group
    2. Comments on a channel post (forwarded to discussion group)
    """
    message = update.message
    if not message:
        return

    chat_id = message.chat_id
    user = message.from_user
    if not user:
        return  # Channel posts without user info

    replied = message.reply_to_message
    giveaway_id = None

    if replied:
        # Case 1: Direct reply to a giveaway post in a group
        giveaway_id = _active_giveaway_posts.get((chat_id, replied.message_id))

        # Case 2: Reply to a channel post forwarded to discussion group
        # When a channel post has comments enabled, the auto-forwarded message
        # in discussion has `reply_to_message.forward_from_chat`
        if not giveaway_id and replied.forward_from_chat:
            channel_id = replied.forward_from_chat.id
            # Try to find by the original channel message
            forward_msg_id = replied.forward_from_message_id
            if forward_msg_id:
                giveaway_id = _channel_post_giveaways.get((channel_id, forward_msg_id))

        # Case 3: The replied message itself IS the forwarded channel post
        # (Telegram auto-forwards channel posts to linked discussion)
        if not giveaway_id and replied.sender_chat:
            sender_chat_id = replied.sender_chat.id
            giveaway_id = _active_giveaway_posts.get((sender_chat_id, replied.message_id))
            if not giveaway_id:
                # Try with forward_from_message_id
                fwd_id = replied.forward_from_message_id
                if fwd_id:
                    giveaway_id = _channel_post_giveaways.get((sender_chat_id, fwd_id))

    # Also check: is this a top-level message in a discussion that's
    # automatically linked to a channel post?
    if not giveaway_id and message.is_topic_message:
        # Topic messages in discussion linked to channel posts
        pass  # Handled above

    if not giveaway_id:
        return

    user_id = user.id

    # Check blacklist
    if await is_blacklisted(user_id):
        return

    # Rate limit check
    if not await check_rate_limit(user_id, "join"):
        return

    async with async_session() as session:
        result = await session.execute(
            select(GroupGiveaway)
            .options(selectinload(GroupGiveaway.entries))
            .where(GroupGiveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

        if not giveaway or giveaway.status != GroupGiveawayStatus.ACTIVE:
            return

        # Check if expired
        if giveaway.ends_at and datetime.utcnow() > giveaway.ends_at:
            return

        # Check one-comment-only rule
        if giveaway.one_comment_only:
            existing = await session.execute(
                select(GroupGiveawayEntry).where(
                    GroupGiveawayEntry.giveaway_id == giveaway_id,
                    GroupGiveawayEntry.user_id == user_id,
                )
            )
            if existing.scalar_one_or_none():
                try:
                    await message.reply_text(
                        "⚠️ You've already entered! Only your first comment counts.",
                        quote=True,
                    )
                except Exception:
                    pass
                return

        # Get comment text
        comment_text = message.text or message.caption or ""

        # Check keyword requirement
        if giveaway.mode == GroupGiveawayMode.KEYWORD and giveaway.keyword:
            if giveaway.keyword.lower() not in comment_text.lower():
                try:
                    await message.reply_text(
                        f"❌ Your comment must include the keyword: "
                        f"<b>{giveaway.keyword}</b>",
                        parse_mode="HTML", quote=True,
                    )
                except Exception:
                    pass
                return

        # Check minimum length
        if len(comment_text.strip()) < giveaway.min_comment_length:
            return

        # Creator cannot enter
        if user_id == giveaway.creator_id:
            return

        # Add entry
        entry = GroupGiveawayEntry(
            giveaway_id=giveaway_id,
            user_id=user_id,
            username=user.username,
            first_name=user.first_name,
            comment_text=comment_text[:500],
            comment_message_id=message.message_id,
            is_valid=True,
        )
        session.add(entry)
        await session.commit()

        entry_count = len([e for e in giveaway.entries if e.is_valid]) + 1

    # Log and award points
    await log_action(user_id, "join")
    await award_points(user_id, "join_giveaway", username=user.username, first_name=user.first_name)

    # Confirm entry
    try:
        await message.reply_text(
            f"✅ Entry #{entry_count} recorded! Good luck 🍀",
            quote=True,
        )
    except Exception:
        pass

    # Auto-pick for FIRST_N mode
    if giveaway.mode == GroupGiveawayMode.FIRST_N and entry_count >= giveaway.winner_count:
        await _auto_complete_first_n(context, giveaway_id, chat_id)



# ─── Draw / Pick / Entries / Cancel (same as before) ─────────────────────────────


async def groupdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Draw winner(s). Command: /groupdraw <id>"""
    user_id = update.effective_user.id

    if not context.args:
        async with async_session() as session:
            result = await session.execute(
                select(GroupGiveaway)
                .options(selectinload(GroupGiveaway.entries))
                .where(
                    GroupGiveaway.creator_id == user_id,
                    GroupGiveaway.status == GroupGiveawayStatus.ACTIVE,
                )
            )
            giveaways = result.scalars().all()

        if not giveaways:
            await update.message.reply_text("You have no active group/channel giveaways.")
            return

        text = "🎯 <b>Your active giveaways:</b>\n\n"
        for gw in giveaways:
            valid = len([e for e in gw.entries if e.is_valid])
            icon = "📢" if gw.is_channel_post else "👥"
            text += f"{icon} <code>/groupdraw {gw.id}</code> — {gw.title} ({valid} entries)\n"
        await update.message.reply_text(text, parse_mode="HTML")
        return

    try:
        giveaway_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")
        return

    async with async_session() as session:
        result = await session.execute(
            select(GroupGiveaway)
            .options(selectinload(GroupGiveaway.entries))
            .where(GroupGiveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

        if not giveaway:
            await update.message.reply_text("❌ Not found.")
            return
        if giveaway.creator_id != user_id:
            await update.message.reply_text("❌ Only the creator can draw.")
            return
        if giveaway.status != GroupGiveawayStatus.ACTIVE:
            await update.message.reply_text("❌ No longer active.")
            return

        valid_entries = [e for e in giveaway.entries if e.is_valid]
        if not valid_entries:
            await update.message.reply_text("❌ No valid entries yet!")
            return

        winner_count = min(giveaway.winner_count, len(valid_entries))
        winners = random.sample(valid_entries, winner_count)

        for w in winners:
            gw_winner = GroupGiveawayWinner(
                giveaway_id=giveaway_id,
                user_id=w.user_id, username=w.username, first_name=w.first_name,
            )
            session.add(gw_winner)
            await award_points(w.user_id, "win_giveaway", username=w.username)

        giveaway.status = GroupGiveawayStatus.COMPLETED
        giveaway.drawn_at = datetime.utcnow()
        await session.commit()

    _active_giveaway_posts.pop((giveaway.chat_id, giveaway.message_id), None)
    _channel_post_giveaways.pop((giveaway.chat_id, giveaway.message_id), None)

    winners_text = "\n".join(
        f"🏆 {i+1}. {'@' + w.username if w.username else w.first_name or f'User {w.user_id}'}"
        for i, w in enumerate(winners)
    )
    await update.message.reply_text(
        f"🎊 <b>GIVEAWAY RESULTS: {giveaway.title}</b>\n\n"
        f"🎁 Prize: {giveaway.prize}\n"
        f"👤 Total entries: {len(valid_entries)}\n\n"
        f"<b>Winners:</b>\n{winners_text}\n\nCongratulations! 🎉",
        parse_mode="HTML",
    )

    # Also announce in the original chat if different
    if update.effective_chat.id != giveaway.chat_id:
        try:
            await context.bot.send_message(
                giveaway.chat_id,
                f"🎊 <b>GIVEAWAY RESULTS: {giveaway.title}</b>\n\n"
                f"<b>Winners:</b>\n{winners_text}\n\nCongratulations! 🎉",
                parse_mode="HTML",
            )
        except Exception:
            pass

    # DM winners
    for w in winners:
        try:
            await context.bot.send_message(
                w.user_id,
                f"🎉 <b>You won!</b>\n\nGiveaway: <b>{giveaway.title}</b>\n"
                f"🎁 Prize: {giveaway.prize}\n\nContact the organizer to claim!",
                parse_mode="HTML",
            )
        except Exception:
            pass



async def _auto_complete_first_n(context: ContextTypes.DEFAULT_TYPE, giveaway_id: int, chat_id: int) -> None:
    """Auto-complete a FIRST_N giveaway when enough entries arrive."""
    async with async_session() as session:
        result = await session.execute(
            select(GroupGiveaway)
            .options(selectinload(GroupGiveaway.entries))
            .where(GroupGiveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()
        if not giveaway or giveaway.status != GroupGiveawayStatus.ACTIVE:
            return

        valid = sorted(
            [e for e in giveaway.entries if e.is_valid],
            key=lambda e: e.entered_at
        )
        winners = valid[:giveaway.winner_count]

        for w in winners:
            gw_winner = GroupGiveawayWinner(
                giveaway_id=giveaway_id,
                user_id=w.user_id, username=w.username, first_name=w.first_name,
            )
            session.add(gw_winner)

        giveaway.status = GroupGiveawayStatus.COMPLETED
        giveaway.drawn_at = datetime.utcnow()
        await session.commit()

    _active_giveaway_posts.pop((chat_id, giveaway.message_id), None)
    _channel_post_giveaways.pop((giveaway.chat_id, giveaway.message_id), None)

    winners_text = "\n".join(
        f"🏆 {i+1}. {'@' + w.username if w.username else w.first_name or f'User {w.user_id}'}"
        for i, w in enumerate(winners)
    )

    try:
        await context.bot.send_message(
            chat_id,
            f"⚡ <b>FIRST {giveaway.winner_count} WIN: {giveaway.title}</b>\n\n"
            f"🎁 Prize: {giveaway.prize}\n\n"
            f"<b>Winners (first to comment):</b>\n{winners_text}\n\n"
            f"Congratulations! 🎉",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def groupentries_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View entries. Command: /groupentries <id>"""
    if not context.args:
        await update.message.reply_text("Usage: /groupentries <giveaway_id>")
        return
    try:
        giveaway_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")
        return

    async with async_session() as session:
        result = await session.execute(
            select(GroupGiveaway).options(selectinload(GroupGiveaway.entries))
            .where(GroupGiveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

    if not giveaway:
        await update.message.reply_text("❌ Not found.")
        return

    valid_entries = sorted([e for e in giveaway.entries if e.is_valid], key=lambda e: e.entered_at)
    if not valid_entries:
        await update.message.reply_text(f"📋 <b>{giveaway.title}</b>\n\nNo entries yet!", parse_mode="HTML")
        return

    text = f"📋 <b>Entries for: {giveaway.title}</b> ({len(valid_entries)} total)\n\n"
    for i, entry in enumerate(valid_entries[:30], 1):
        name = f"@{entry.username}" if entry.username else (entry.first_name or f"User {entry.user_id}")
        preview = (entry.comment_text[:40] + "...") if entry.comment_text and len(entry.comment_text) > 40 else (entry.comment_text or "")
        text += f"{i}. {name} — \"{preview}\"\n"
    if len(valid_entries) > 30:
        text += f"\n... and {len(valid_entries) - 30} more"
    text += f"\n\nUse <code>/grouppick {giveaway_id} &lt;num&gt;</code> to pick."
    await update.message.reply_text(text, parse_mode="HTML")



async def grouppick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick specific winner. Command: /grouppick <id> <entry_number>"""
    user_id = update.effective_user.id
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /grouppick <giveaway_id> <entry_number>")
        return
    try:
        giveaway_id = int(context.args[0])
        entry_num = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid numbers.")
        return

    async with async_session() as session:
        result = await session.execute(
            select(GroupGiveaway).options(selectinload(GroupGiveaway.entries))
            .where(GroupGiveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()
        if not giveaway:
            await update.message.reply_text("❌ Not found.")
            return
        if giveaway.creator_id != user_id:
            await update.message.reply_text("❌ Only the creator can pick.")
            return
        if giveaway.status != GroupGiveawayStatus.ACTIVE:
            await update.message.reply_text("❌ No longer active.")
            return

        valid_entries = sorted([e for e in giveaway.entries if e.is_valid], key=lambda e: e.entered_at)
        if entry_num < 1 or entry_num > len(valid_entries):
            await update.message.reply_text(f"❌ Valid range: 1–{len(valid_entries)}")
            return

        winner_entry = valid_entries[entry_num - 1]
        gw_winner = GroupGiveawayWinner(
            giveaway_id=giveaway_id,
            user_id=winner_entry.user_id, username=winner_entry.username, first_name=winner_entry.first_name,
        )
        session.add(gw_winner)
        giveaway.status = GroupGiveawayStatus.COMPLETED
        giveaway.drawn_at = datetime.utcnow()
        await session.commit()

    _active_giveaway_posts.pop((giveaway.chat_id, giveaway.message_id), None)
    _channel_post_giveaways.pop((giveaway.chat_id, giveaway.message_id), None)

    winner_name = f"@{winner_entry.username}" if winner_entry.username else (winner_entry.first_name or f"User {winner_entry.user_id}")
    await update.message.reply_text(
        f"🎯 <b>WINNER PICKED: {giveaway.title}</b>\n\n"
        f"🏆 {winner_name}\n💬 \"{winner_entry.comment_text[:100]}\"\n"
        f"🎁 Prize: {giveaway.prize}\n\nCongratulations! 🎉",
        parse_mode="HTML",
    )
    try:
        await context.bot.send_message(winner_entry.user_id,
            f"🎉 <b>You won!</b>\n\nGiveaway: <b>{giveaway.title}</b>\n🎁 Prize: {giveaway.prize}",
            parse_mode="HTML")
    except Exception:
        pass


async def cancelgroupgiveaway_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel. Command: /cancelgroupgiveaway <id>"""
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /cancelgroupgiveaway <id>")
        return
    try:
        giveaway_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")
        return
    async with async_session() as session:
        result = await session.execute(select(GroupGiveaway).where(GroupGiveaway.id == giveaway_id))
        giveaway = result.scalar_one_or_none()
        if not giveaway:
            await update.message.reply_text("❌ Not found.")
            return
        if giveaway.creator_id != user_id:
            await update.message.reply_text("❌ Only the creator can cancel.")
            return
        if giveaway.status != GroupGiveawayStatus.ACTIVE:
            await update.message.reply_text("❌ Already finished.")
            return
        giveaway.status = GroupGiveawayStatus.CANCELLED
        await session.commit()
    _active_giveaway_posts.pop((giveaway.chat_id, giveaway.message_id), None)
    _channel_post_giveaways.pop((giveaway.chat_id, giveaway.message_id), None)
    await update.message.reply_text(f"❌ Giveaway <b>'{giveaway.title}'</b> cancelled.", parse_mode="HTML")



# ─── Handler Registration ────────────────────────────────────────────────────────


def get_group_giveaway_handlers() -> list:
    """Return group/channel giveaway handlers."""
    create_conv = ConversationHandler(
        entry_points=[
            CommandHandler("groupgiveaway", groupgiveaway_start),
            CommandHandler("channelgiveaway", channelgiveaway_start),
        ],
        states={
            GG_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, gg_title)],
            GG_PRIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, gg_prize)],
            GG_MODE: [CallbackQueryHandler(gg_mode_selected, pattern=r"^ggmode_")],
            GG_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, gg_keyword)],
            GG_WINNERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, gg_winners)],
            GG_DURATION: [CallbackQueryHandler(gg_duration, pattern=r"^ggdur_")],
        },
        fallbacks=[CommandHandler("cancel", gg_cancel)],
    )

    return [
        create_conv,
        CommandHandler("groupdraw", groupdraw_command),
        CommandHandler("groupentries", groupentries_command),
        CommandHandler("grouppick", grouppick_command),
        CommandHandler("cancelgroupgiveaway", cancelgroupgiveaway_command),
        # Reply/comment handler — catches replies to giveaway posts
        MessageHandler(
            filters.REPLY & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_group_reply,
        ),
    ]
