"""Group & Channel comment-based giveaway handlers.

Supports:
- Groups: Users reply to a giveaway post to enter
- Channels: Admin posts giveaway, users comment in discussion to enter
- Reactions: Users react to the post with any emoji to enter (REACTION mode)

Bot picks winners from participants.
"""

import random
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    MessageReactionHandler,
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
from bot.utils.subscription import get_unsubscribed, parse_channels, serialize_channels
from bot.utils.referral import verify_pending_referrals


# Conversation states for group giveaway creation
GG_TITLE, GG_PRIZE, GG_MODE, GG_KEYWORD, GG_WINNERS, GG_CHANNELS, GG_DURATION = range(7)

# Store active group giveaways:
# Key: (chat_id, message_id) → giveaway_id
# For channels: also map (discussion_group_id, forwarded_msg_id) → giveaway_id
_active_giveaway_posts: dict[tuple[int, int], int] = {}

# Map channel_post_id → giveaway_id (for channel comment tracking)
_channel_post_giveaways: dict[tuple[int, int], int] = {}


def _format_name(entry) -> str:
    """Human-readable name for an entry/winner."""
    if entry.username:
        return f"@{entry.username}"
    return entry.first_name or f"User {entry.user_id}"


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

    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    # Allow: groups, supergroups, and channels
    if chat_type == "private":
        await update.message.reply_text(get_text("gg_use_in_group", lang=lang), parse_mode="HTML")
        return ConversationHandler.END

    context.user_data["gg_lang"] = lang
    context.user_data["gg_chat_id"] = chat.id
    context.user_data["gg_is_channel"] = (chat_type == "channel")

    source = get_text(
        "gg_source_channel" if chat_type == "channel" else "gg_source_group",
        lang=lang,
    )
    await update.message.reply_text(
        get_text("gg_create_intro", lang=lang, source=source), parse_mode="HTML"
    )
    return GG_TITLE


async def channelgiveaway_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start channel giveaway (alias). Command: /channelgiveaway"""
    chat = update.effective_chat
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    if chat.type == "private":
        await update.message.reply_text(get_text("gg_use_in_channel", lang=lang), parse_mode="HTML")
        return ConversationHandler.END

    context.user_data["gg_lang"] = lang
    context.user_data["gg_chat_id"] = chat.id
    context.user_data["gg_is_channel"] = True

    await update.message.reply_text(get_text("gg_channel_intro", lang=lang), parse_mode="HTML")
    return GG_TITLE



async def gg_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive title."""
    lang = context.user_data.get("gg_lang", "en")
    context.user_data["gg_title"] = update.message.text.strip()
    await update.message.reply_text(get_text("gg_ask_prize", lang=lang), parse_mode="HTML")
    return GG_PRIZE


async def gg_prize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive prize."""
    lang = context.user_data.get("gg_lang", "en")
    context.user_data["gg_prize"] = update.message.text.strip()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(get_text("gg_mode_random_btn", lang=lang), callback_data="ggmode_random")],
        [InlineKeyboardButton(get_text("gg_mode_first_n_btn", lang=lang), callback_data="ggmode_first_n")],
        [InlineKeyboardButton(get_text("gg_mode_keyword_btn", lang=lang), callback_data="ggmode_keyword")],
        [InlineKeyboardButton(get_text("gg_mode_reaction_btn", lang=lang), callback_data="ggmode_reaction")],
    ])

    await update.message.reply_text(
        get_text("gg_ask_mode", lang=lang), reply_markup=keyboard, parse_mode="HTML"
    )
    return GG_MODE


async def gg_mode_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle mode selection."""
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("gg_lang", "en")

    mode_map = {
        "ggmode_random": GroupGiveawayMode.RANDOM,
        "ggmode_first_n": GroupGiveawayMode.FIRST_N,
        "ggmode_keyword": GroupGiveawayMode.KEYWORD,
        "ggmode_reaction": GroupGiveawayMode.REACTION,
    }
    context.user_data["gg_mode"] = mode_map[query.data]

    if query.data == "ggmode_keyword":
        await query.edit_message_text(get_text("gg_ask_keyword", lang=lang), parse_mode="HTML")
        return GG_KEYWORD
    else:
        context.user_data["gg_keyword"] = None
        await query.edit_message_text(get_text("gg_ask_winners", lang=lang), parse_mode="HTML")
        return GG_WINNERS


async def gg_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive keyword."""
    lang = context.user_data.get("gg_lang", "en")
    context.user_data["gg_keyword"] = update.message.text.strip()
    await update.message.reply_text(get_text("gg_ask_winners", lang=lang), parse_mode="HTML")
    return GG_WINNERS


async def gg_winners(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive winner count."""
    lang = context.user_data.get("gg_lang", "en")
    try:
        count = int(update.message.text.strip())
        if count < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text(get_text("gg_invalid_number", lang=lang))
        return GG_WINNERS

    context.user_data["gg_winner_count"] = count
    await update.message.reply_text(get_text("gg_ask_channels", lang=lang), parse_mode="HTML")
    return GG_CHANNELS


async def gg_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the (optional) required subscription channels."""
    lang = context.user_data.get("gg_lang", "en")
    text = update.message.text.strip()

    if text.lower() == "/skip":
        context.user_data["gg_channels"] = None
    else:
        context.user_data["gg_channels"] = serialize_channels(parse_channels(text))

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(get_text("dur_1h", lang=lang), callback_data="ggdur_1h"),
            InlineKeyboardButton(get_text("dur_6h", lang=lang), callback_data="ggdur_6h"),
        ],
        [
            InlineKeyboardButton(get_text("dur_12h", lang=lang), callback_data="ggdur_12h"),
            InlineKeyboardButton(get_text("dur_24h", lang=lang), callback_data="ggdur_24h"),
        ],
        [
            InlineKeyboardButton(get_text("dur_3d", lang=lang), callback_data="ggdur_3d"),
            InlineKeyboardButton(get_text("dur_7d", lang=lang), callback_data="ggdur_7d"),
        ],
        [InlineKeyboardButton(get_text("dur_none", lang=lang), callback_data="ggdur_none")],
    ])

    await update.message.reply_text(
        get_text("gg_ask_duration", lang=lang), reply_markup=keyboard
    )
    return GG_DURATION



async def gg_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle duration and create the giveaway post."""
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("gg_lang", "en")

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
    required_channels = context.user_data.get("gg_channels")

    # Save to database
    async with async_session() as session:
        giveaway = GroupGiveaway(
            title=context.user_data["gg_title"],
            prize=context.user_data["gg_prize"],
            winner_count=context.user_data["gg_winner_count"],
            mode=mode,
            keyword=keyword,
            required_channels=required_channels,
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
        GroupGiveawayMode.RANDOM: get_text("gg_lbl_random", lang=lang),
        GroupGiveawayMode.FIRST_N: get_text("gg_lbl_first_n", lang=lang, count=giveaway.winner_count),
        GroupGiveawayMode.KEYWORD: get_text("gg_lbl_keyword", lang=lang, keyword=keyword),
        GroupGiveawayMode.REACTION: get_text("gg_lbl_reaction", lang=lang),
    }
    end_text = (
        get_text("gg_ends_at", lang=lang, time=ends_at.strftime("%Y-%m-%d %H:%M UTC"))
        if ends_at else get_text("gg_no_limit", lang=lang)
    )
    keyword_text = get_text("gg_kw_required_line", lang=lang, keyword=keyword) if keyword else ""
    source_icon = "📢" if is_channel else "🎯"
    how_to_enter = get_text(
        "gg_how_react" if mode == GroupGiveawayMode.REACTION else "gg_how_comment",
        lang=lang,
    )

    announcement = get_text(
        "gg_announcement", lang=lang,
        icon=source_icon,
        title=giveaway.title,
        prize=giveaway.prize,
        winners=giveaway.winner_count,
        mode=mode_labels[mode],
        keyword_text=keyword_text,
        end_text=end_text,
        how_to_enter=how_to_enter,
        id=giveaway.id,
    )

    # Delete the conversation message
    try:
        await query.message.delete()
    except Exception:
        pass

    # Send the giveaway post
    sent_message = await context.bot.send_message(
        chat_id=chat_id, text=announcement, parse_mode="HTML",
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
    lang = context.user_data.get("gg_lang", "en")
    context.user_data.clear()
    await update.message.reply_text(get_text("gg_cancelled", lang=lang))
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
        if not giveaway_id and replied.forward_from_chat:
            channel_id = replied.forward_from_chat.id
            forward_msg_id = replied.forward_from_message_id
            if forward_msg_id:
                giveaway_id = _channel_post_giveaways.get((channel_id, forward_msg_id))

        # Case 3: The replied message itself IS the forwarded channel post
        if not giveaway_id and replied.sender_chat:
            sender_chat_id = replied.sender_chat.id
            giveaway_id = _active_giveaway_posts.get((sender_chat_id, replied.message_id))
            if not giveaway_id:
                fwd_id = replied.forward_from_message_id
                if fwd_id:
                    giveaway_id = _channel_post_giveaways.get((sender_chat_id, fwd_id))

    if not giveaway_id:
        return

    user_id = user.id
    lang = await get_user_lang(user_id)

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
                    await message.reply_text(get_text("gg_already_entered", lang=lang), quote=True)
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
                        get_text("gg_keyword_required", lang=lang, keyword=giveaway.keyword),
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

        # Enforce channel subscription (forced-sub) before recording the entry
        required_channels = parse_channels(giveaway.required_channels)
        if required_channels:
            missing = await get_unsubscribed(context.bot, user_id, required_channels)
            if missing:
                try:
                    await message.reply_text(
                        get_text("gg_must_subscribe_comment", lang=lang, channels=", ".join(missing)),
                        parse_mode="HTML", quote=True,
                    )
                except Exception:
                    pass
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
    # Entering proves channel membership — credit any pending referral.
    await verify_pending_referrals(context.bot, user_id)

    # Confirm entry
    try:
        await message.reply_text(get_text("gg_entry_recorded", lang=lang, count=entry_count), quote=True)
    except Exception:
        pass

    # Auto-pick for FIRST_N mode
    if giveaway.mode == GroupGiveawayMode.FIRST_N and entry_count >= giveaway.winner_count:
        await _auto_complete_first_n(context, giveaway_id, chat_id, lang)



# ─── Handle Reactions (REACTION mode) ────────────────────────────────────────────


async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record a reaction on a giveaway post as an entry (REACTION mode)."""
    reaction = update.message_reaction
    if not reaction:
        return

    chat_id = reaction.chat.id
    message_id = reaction.message_id
    giveaway_id = _active_giveaway_posts.get((chat_id, message_id))
    if not giveaway_id:
        return

    user = reaction.user
    if not user:
        return  # Anonymous / channel reactions carry no user

    # Only count when a reaction was ADDED (new_reaction non-empty).
    if not reaction.new_reaction:
        return

    user_id = user.id
    lang = await get_user_lang(user_id)

    if await is_blacklisted(user_id):
        return
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
        if giveaway.mode != GroupGiveawayMode.REACTION:
            return
        if giveaway.ends_at and datetime.utcnow() > giveaway.ends_at:
            return
        if user_id == giveaway.creator_id:
            return

        # One entry per user
        existing = await session.execute(
            select(GroupGiveawayEntry).where(
                GroupGiveawayEntry.giveaway_id == giveaway_id,
                GroupGiveawayEntry.user_id == user_id,
            )
        )
        if existing.scalar_one_or_none():
            return

        # Enforce channel subscription (forced-sub)
        required_channels = parse_channels(giveaway.required_channels)
        if required_channels:
            missing = await get_unsubscribed(context.bot, user_id, required_channels)
            if missing:
                try:
                    await context.bot.send_message(
                        user_id,
                        get_text(
                            "gg_must_subscribe_react", lang=lang,
                            title=giveaway.title, channels=", ".join(missing),
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                return

        entry = GroupGiveawayEntry(
            giveaway_id=giveaway_id,
            user_id=user_id,
            username=user.username,
            first_name=user.first_name,
            comment_text="[reaction]",
            comment_message_id=message_id,
            is_valid=True,
        )
        session.add(entry)
        await session.commit()

    await log_action(user_id, "join")
    await award_points(user_id, "join_giveaway", username=user.username, first_name=user.first_name)



# ─── Draw / Pick / Entries / Cancel ──────────────────────────────────────────────


async def groupdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Draw winner(s). Command: /groupdraw <id>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

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
            await update.message.reply_text(get_text("gg_no_active", lang=lang))
            return

        text = get_text("gg_active_header", lang=lang)
        for gw in giveaways:
            valid = len([e for e in gw.entries if e.is_valid])
            icon = "📢" if gw.is_channel_post else "👥"
            text += f"{icon} <code>/groupdraw {gw.id}</code> — {gw.title} ({valid})\n"
        await update.message.reply_text(text, parse_mode="HTML")
        return

    try:
        giveaway_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(get_text("gg_invalid_id", lang=lang))
        return

    async with async_session() as session:
        result = await session.execute(
            select(GroupGiveaway)
            .options(selectinload(GroupGiveaway.entries))
            .where(GroupGiveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

        if not giveaway:
            await update.message.reply_text(get_text("gg_not_found", lang=lang))
            return
        if giveaway.creator_id != user_id:
            await update.message.reply_text(get_text("gg_only_creator_draw", lang=lang))
            return
        if giveaway.status != GroupGiveawayStatus.ACTIVE:
            await update.message.reply_text(get_text("gg_not_active", lang=lang))
            return

        valid_entries = [e for e in giveaway.entries if e.is_valid]
        if not valid_entries:
            await update.message.reply_text(get_text("gg_no_valid_entries", lang=lang))
            return

        winner_count = min(giveaway.winner_count, len(valid_entries))
        winners = random.sample(valid_entries, winner_count)

        for w in winners:
            session.add(GroupGiveawayWinner(
                giveaway_id=giveaway_id,
                user_id=w.user_id, username=w.username, first_name=w.first_name,
            ))
            await award_points(w.user_id, "win_giveaway", username=w.username)

        giveaway.status = GroupGiveawayStatus.COMPLETED
        giveaway.drawn_at = datetime.utcnow()
        await session.commit()

    _active_giveaway_posts.pop((giveaway.chat_id, giveaway.message_id), None)
    _channel_post_giveaways.pop((giveaway.chat_id, giveaway.message_id), None)

    winners_text = "\n".join(
        f"🏆 {i+1}. {_format_name(w)}" for i, w in enumerate(winners)
    )
    await update.message.reply_text(
        get_text(
            "gg_results", lang=lang,
            title=giveaway.title, prize=giveaway.prize,
            total=len(valid_entries), winners=winners_text,
        ),
        parse_mode="HTML",
    )

    # Also announce in the original chat if different
    if update.effective_chat.id != giveaway.chat_id:
        try:
            await context.bot.send_message(
                giveaway.chat_id,
                get_text("gg_results_short", lang=lang, title=giveaway.title, winners=winners_text),
                parse_mode="HTML",
            )
        except Exception:
            pass

    # DM winners
    for w in winners:
        w_lang = await get_user_lang(w.user_id)
        try:
            await context.bot.send_message(
                w.user_id,
                get_text("gg_winner_dm", lang=w_lang, title=giveaway.title, prize=giveaway.prize),
                parse_mode="HTML",
            )
        except Exception:
            pass



async def _auto_complete_first_n(
    context: ContextTypes.DEFAULT_TYPE, giveaway_id: int, chat_id: int, lang: str = "en"
) -> None:
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
            session.add(GroupGiveawayWinner(
                giveaway_id=giveaway_id,
                user_id=w.user_id, username=w.username, first_name=w.first_name,
            ))

        giveaway.status = GroupGiveawayStatus.COMPLETED
        giveaway.drawn_at = datetime.utcnow()
        await session.commit()

    _active_giveaway_posts.pop((chat_id, giveaway.message_id), None)
    _channel_post_giveaways.pop((giveaway.chat_id, giveaway.message_id), None)

    winners_text = "\n".join(
        f"🏆 {i+1}. {_format_name(w)}" for i, w in enumerate(winners)
    )
    try:
        await context.bot.send_message(
            chat_id,
            get_text(
                "gg_first_n_results", lang=lang,
                count=giveaway.winner_count, title=giveaway.title,
                prize=giveaway.prize, winners=winners_text,
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def groupentries_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View entries. Command: /groupentries <id>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    if not context.args:
        await update.message.reply_text(get_text("gg_entries_usage", lang=lang))
        return
    try:
        giveaway_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(get_text("gg_invalid_id", lang=lang))
        return

    async with async_session() as session:
        result = await session.execute(
            select(GroupGiveaway).options(selectinload(GroupGiveaway.entries))
            .where(GroupGiveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

    if not giveaway:
        await update.message.reply_text(get_text("gg_not_found", lang=lang))
        return

    valid_entries = sorted([e for e in giveaway.entries if e.is_valid], key=lambda e: e.entered_at)
    if not valid_entries:
        await update.message.reply_text(
            get_text("gg_no_entries_yet", lang=lang, title=giveaway.title), parse_mode="HTML"
        )
        return

    text = get_text("gg_entries_header", lang=lang, title=giveaway.title, count=len(valid_entries))
    for i, entry in enumerate(valid_entries[:30], 1):
        name = _format_name(entry)
        preview = (entry.comment_text[:40] + "...") if entry.comment_text and len(entry.comment_text) > 40 else (entry.comment_text or "")
        text += f"{i}. {name} — \"{preview}\"\n"
    if len(valid_entries) > 30:
        text += get_text("gg_entries_more", lang=lang, count=len(valid_entries) - 30)
    text += get_text("gg_pick_hint", lang=lang, id=giveaway_id)
    await update.message.reply_text(text, parse_mode="HTML")



async def grouppick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick specific winner. Command: /grouppick <id> <entry_number>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(get_text("gg_pick_usage", lang=lang))
        return
    try:
        giveaway_id = int(context.args[0])
        entry_num = int(context.args[1])
    except ValueError:
        await update.message.reply_text(get_text("gg_invalid_numbers", lang=lang))
        return

    async with async_session() as session:
        result = await session.execute(
            select(GroupGiveaway).options(selectinload(GroupGiveaway.entries))
            .where(GroupGiveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()
        if not giveaway:
            await update.message.reply_text(get_text("gg_not_found", lang=lang))
            return
        if giveaway.creator_id != user_id:
            await update.message.reply_text(get_text("gg_only_creator_pick", lang=lang))
            return
        if giveaway.status != GroupGiveawayStatus.ACTIVE:
            await update.message.reply_text(get_text("gg_not_active", lang=lang))
            return

        valid_entries = sorted([e for e in giveaway.entries if e.is_valid], key=lambda e: e.entered_at)
        if entry_num < 1 or entry_num > len(valid_entries):
            await update.message.reply_text(get_text("gg_pick_range", lang=lang, max=len(valid_entries)))
            return

        winner_entry = valid_entries[entry_num - 1]
        session.add(GroupGiveawayWinner(
            giveaway_id=giveaway_id,
            user_id=winner_entry.user_id, username=winner_entry.username, first_name=winner_entry.first_name,
        ))
        giveaway.status = GroupGiveawayStatus.COMPLETED
        giveaway.drawn_at = datetime.utcnow()
        await session.commit()

    _active_giveaway_posts.pop((giveaway.chat_id, giveaway.message_id), None)
    _channel_post_giveaways.pop((giveaway.chat_id, giveaway.message_id), None)

    winner_name = _format_name(winner_entry)
    await update.message.reply_text(
        get_text(
            "gg_pick_result", lang=lang,
            title=giveaway.title, name=winner_name,
            comment=(winner_entry.comment_text or "")[:100], prize=giveaway.prize,
        ),
        parse_mode="HTML",
    )
    w_lang = await get_user_lang(winner_entry.user_id)
    try:
        await context.bot.send_message(
            winner_entry.user_id,
            get_text("gg_winner_dm", lang=w_lang, title=giveaway.title, prize=giveaway.prize),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def cancelgroupgiveaway_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel. Command: /cancelgroupgiveaway <id>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    if not context.args:
        await update.message.reply_text(get_text("gg_cancel_usage", lang=lang))
        return
    try:
        giveaway_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(get_text("gg_invalid_id", lang=lang))
        return
    async with async_session() as session:
        result = await session.execute(select(GroupGiveaway).where(GroupGiveaway.id == giveaway_id))
        giveaway = result.scalar_one_or_none()
        if not giveaway:
            await update.message.reply_text(get_text("gg_not_found", lang=lang))
            return
        if giveaway.creator_id != user_id:
            await update.message.reply_text(get_text("gg_only_creator_cancel", lang=lang))
            return
        if giveaway.status != GroupGiveawayStatus.ACTIVE:
            await update.message.reply_text(get_text("gg_already_finished", lang=lang))
            return
        giveaway.status = GroupGiveawayStatus.CANCELLED
        await session.commit()
    _active_giveaway_posts.pop((giveaway.chat_id, giveaway.message_id), None)
    _channel_post_giveaways.pop((giveaway.chat_id, giveaway.message_id), None)
    await update.message.reply_text(
        get_text("gg_cancel_done", lang=lang, title=giveaway.title), parse_mode="HTML"
    )



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
            GG_CHANNELS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, gg_channels),
                CommandHandler("skip", gg_channels),
            ],
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
        # Reaction handler — catches emoji reactions on giveaway posts (REACTION mode)
        MessageReactionHandler(handle_reaction),
    ]
