"""Giveaway command handlers with i18n support."""

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
    filters,
)

from bot.i18n import get_text
from bot.models import (
    Giveaway,
    GiveawayParticipant,
    GiveawayStatus,
    GiveawayWinner,
    async_session,
)
from bot.utils.lang import get_user_lang, t

# Conversation states
TITLE, DESCRIPTION, PRIZE, WINNER_COUNT, DURATION = range(5)


# ─── Create Giveaway ──────────────────────────────────────────────────────────


async def new_giveaway_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the giveaway creation flow. Command: /newgiveaway"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    context.user_data["lang"] = lang
    text = get_text("gw_create_title", lang=lang)
    await update.message.reply_text(text, parse_mode="HTML")
    return TITLE


async def giveaway_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive giveaway title."""
    lang = context.user_data.get("lang", "en")
    context.user_data["giveaway_title"] = update.message.text.strip()
    text = get_text("gw_create_description", lang=lang)
    await update.message.reply_text(text, parse_mode="HTML")
    return DESCRIPTION


async def giveaway_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive giveaway description."""
    lang = context.user_data.get("lang", "en")
    text = update.message.text.strip()
    if text.lower() == "/skip":
        context.user_data["giveaway_description"] = None
    else:
        context.user_data["giveaway_description"] = text

    msg = get_text("gw_create_prize", lang=lang)
    await update.message.reply_text(msg, parse_mode="HTML")
    return PRIZE


async def giveaway_prize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the prize."""
    lang = context.user_data.get("lang", "en")
    context.user_data["giveaway_prize"] = update.message.text.strip()
    msg = get_text("gw_create_winners", lang=lang)
    await update.message.reply_text(msg, parse_mode="HTML")
    return WINNER_COUNT


async def giveaway_winner_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive winner count."""
    lang = context.user_data.get("lang", "en")
    try:
        count = int(update.message.text.strip())
        if count < 1:
            raise ValueError
    except ValueError:
        msg = get_text("gw_invalid_number", lang=lang)
        await update.message.reply_text(msg)
        return WINNER_COUNT

    context.user_data["giveaway_winner_count"] = count

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(get_text("dur_1h", lang=lang), callback_data="dur_1h"),
            InlineKeyboardButton(get_text("dur_6h", lang=lang), callback_data="dur_6h"),
        ],
        [
            InlineKeyboardButton(get_text("dur_12h", lang=lang), callback_data="dur_12h"),
            InlineKeyboardButton(get_text("dur_24h", lang=lang), callback_data="dur_24h"),
        ],
        [
            InlineKeyboardButton(get_text("dur_3d", lang=lang), callback_data="dur_3d"),
            InlineKeyboardButton(get_text("dur_7d", lang=lang), callback_data="dur_7d"),
        ],
        [InlineKeyboardButton(get_text("dur_none", lang=lang), callback_data="dur_none")],
    ])

    msg = get_text("gw_create_duration", lang=lang)
    await update.message.reply_text(msg, reply_markup=keyboard)
    return DURATION


async def giveaway_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle duration selection."""
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")

    duration_map = {
        "dur_1h": timedelta(hours=1),
        "dur_6h": timedelta(hours=6),
        "dur_12h": timedelta(hours=12),
        "dur_24h": timedelta(hours=24),
        "dur_3d": timedelta(days=3),
        "dur_7d": timedelta(days=7),
        "dur_none": None,
    }

    duration = duration_map.get(query.data)
    ends_at = datetime.utcnow() + duration if duration else None

    # Save to database
    async with async_session() as session:
        giveaway = Giveaway(
            title=context.user_data["giveaway_title"],
            description=context.user_data.get("giveaway_description"),
            prize=context.user_data["giveaway_prize"],
            winner_count=context.user_data["giveaway_winner_count"],
            creator_id=query.from_user.id,
            creator_username=query.from_user.username,
            chat_id=query.message.chat_id,
            ends_at=ends_at,
        )
        session.add(giveaway)
        await session.commit()
        await session.refresh(giveaway)

    # Build announcement
    end_text = (
        get_text("gw_ends_at", lang=lang, time=ends_at.strftime("%Y-%m-%d %H:%M UTC"))
        if ends_at
        else get_text("gw_no_limit", lang=lang)
    )
    desc_text = f"\n📝 {giveaway.description}" if giveaway.description else ""

    join_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            get_text("gw_join_button", lang=lang),
            callback_data=f"join_gw_{giveaway.id}"
        )]
    ])

    announcement = get_text(
        "gw_announcement", lang=lang,
        title=giveaway.title,
        description=desc_text,
        prize=giveaway.prize,
        winner_count=giveaway.winner_count,
        end_text=end_text,
        participants=0,
    )

    await query.edit_message_text(
        announcement, reply_markup=join_keyboard, parse_mode="HTML"
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel giveaway creation."""
    user_id = update.effective_user.id
    text = await t("gw_cancelled", user_id)
    context.user_data.clear()
    await update.message.reply_text(text)
    return ConversationHandler.END


# ─── Join Giveaway ──────────────────────────────────────────────────────────────


async def join_giveaway_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'Join Giveaway' button."""
    query = update.callback_query
    giveaway_id = int(query.data.split("_")[-1])
    user = query.from_user
    lang = await get_user_lang(user.id)

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway)
            .options(selectinload(Giveaway.participants))
            .where(Giveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

        if not giveaway:
            await query.answer(get_text("gw_not_found", lang=lang), show_alert=True)
            return

        if giveaway.status != GiveawayStatus.ACTIVE:
            await query.answer(get_text("gw_ended", lang=lang), show_alert=True)
            return

        if giveaway.ends_at and datetime.utcnow() > giveaway.ends_at:
            await query.answer(get_text("gw_expired", lang=lang), show_alert=True)
            return

        # Check already joined
        existing = await session.execute(
            select(GiveawayParticipant).where(
                GiveawayParticipant.giveaway_id == giveaway_id,
                GiveawayParticipant.user_id == user.id,
            )
        )
        if existing.scalar_one_or_none():
            await query.answer(get_text("gw_already_joined", lang=lang), show_alert=True)
            return

        # Add participant
        participant = GiveawayParticipant(
            giveaway_id=giveaway_id,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
        )
        session.add(participant)
        await session.commit()

        # Refresh count
        result = await session.execute(
            select(Giveaway)
            .options(selectinload(Giveaway.participants))
            .where(Giveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one()
        participant_count = len(giveaway.participants)

    await query.answer(get_text("gw_joined", lang=lang, count=participant_count))

    # Update message
    end_text = (
        get_text("gw_ends_at", lang=lang, time=giveaway.ends_at.strftime("%Y-%m-%d %H:%M UTC"))
        if giveaway.ends_at
        else get_text("gw_no_limit", lang=lang)
    )
    desc_text = f"\n📝 {giveaway.description}" if giveaway.description else ""

    join_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            get_text("gw_join_button", lang=lang),
            callback_data=f"join_gw_{giveaway.id}"
        )]
    ])

    announcement = get_text(
        "gw_announcement", lang=lang,
        title=giveaway.title,
        description=desc_text,
        prize=giveaway.prize,
        winner_count=giveaway.winner_count,
        end_text=end_text,
        participants=participant_count,
    )

    await query.edit_message_text(
        announcement, reply_markup=join_keyboard, parse_mode="HTML"
    )


# ─── Draw Winners ───────────────────────────────────────────────────────────────


async def draw_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Draw winners for a giveaway. Command: /draw <giveaway_id>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    if not context.args:
        async with async_session() as session:
            result = await session.execute(
                select(Giveaway).where(
                    Giveaway.creator_id == user_id,
                    Giveaway.status == GiveawayStatus.ACTIVE,
                )
            )
            giveaways = result.scalars().all()

        if not giveaways:
            await update.message.reply_text(get_text("gw_no_active", lang=lang))
            return

        gw_list = "\n".join(f"• <code>/draw {gw.id}</code> — {gw.title}" for gw in giveaways)
        text = get_text("gw_active_list", lang=lang, list=gw_list)
        await update.message.reply_text(text, parse_mode="HTML")
        return

    try:
        giveaway_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(get_text("gw_not_found", lang=lang))
        return

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway)
            .options(selectinload(Giveaway.participants))
            .where(Giveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

        if not giveaway:
            await update.message.reply_text(get_text("gw_not_found", lang=lang))
            return

        if giveaway.creator_id != user_id:
            await update.message.reply_text(get_text("gw_only_creator", lang=lang))
            return

        if giveaway.status != GiveawayStatus.ACTIVE:
            await update.message.reply_text(get_text("gw_not_active", lang=lang))
            return

        participants = giveaway.participants
        if not participants:
            await update.message.reply_text(get_text("gw_no_participants", lang=lang))
            return

        # Draw winners
        winner_count = min(giveaway.winner_count, len(participants))
        winners = random.sample(participants, winner_count)

        for winner in winners:
            gw_winner = GiveawayWinner(
                giveaway_id=giveaway_id,
                user_id=winner.user_id,
                username=winner.username,
                first_name=winner.first_name,
            )
            session.add(gw_winner)

        giveaway.status = GiveawayStatus.COMPLETED
        giveaway.drawn_at = datetime.utcnow()
        await session.commit()

    winners_text = "\n".join(
        f"🏆 {i+1}. {_format_user(w)}" for i, w in enumerate(winners)
    )

    result_text = get_text(
        "gw_results", lang=lang,
        title=giveaway.title,
        prize=giveaway.prize,
        total=len(participants),
        winners=winners_text,
    )
    await update.message.reply_text(result_text, parse_mode="HTML")


# ─── My Giveaways ───────────────────────────────────────────────────────────────


async def my_giveaways(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List user's giveaways. Command: /mygiveaways"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway)
            .options(selectinload(Giveaway.participants))
            .where(Giveaway.creator_id == user_id)
            .order_by(Giveaway.created_at.desc())
            .limit(10)
        )
        giveaways = result.scalars().all()

    if not giveaways:
        await update.message.reply_text(get_text("gw_my_list_empty", lang=lang))
        return

    status_emoji = {
        GiveawayStatus.ACTIVE: "🟢",
        GiveawayStatus.COMPLETED: "✅",
        GiveawayStatus.CANCELLED: "❌",
    }

    text = get_text("gw_my_list_header", lang=lang)
    for gw in giveaways:
        emoji = status_emoji.get(gw.status, "❓")
        text += (
            f"{emoji} <b>{gw.title}</b> (ID: {gw.id})\n"
            f"   🎁 {gw.prize} | 👤 {len(gw.participants)}\n"
            f"   Status: {gw.status.value}\n\n"
        )

    await update.message.reply_text(text, parse_mode="HTML")


# ─── Cancel Giveaway ─────────────────────────────────────────────────────────────


async def cancel_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a giveaway. Command: /cancelgiveaway <id>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    if not context.args:
        await update.message.reply_text(get_text("gw_cancel_usage", lang=lang))
        return

    try:
        giveaway_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(get_text("gw_not_found", lang=lang))
        return

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).where(Giveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

        if not giveaway:
            await update.message.reply_text(get_text("gw_not_found", lang=lang))
            return

        if giveaway.creator_id != user_id:
            await update.message.reply_text(get_text("gw_cancel_only_creator", lang=lang))
            return

        if giveaway.status != GiveawayStatus.ACTIVE:
            await update.message.reply_text(get_text("gw_cancel_not_active", lang=lang))
            return

        giveaway.status = GiveawayStatus.CANCELLED
        await session.commit()

    text = get_text("gw_cancel_done", lang=lang, title=giveaway.title)
    await update.message.reply_text(text, parse_mode="HTML")


# ─── Helpers ─────────────────────────────────────────────────────────────────────


def _format_user(participant) -> str:
    """Format a participant/winner for display."""
    if participant.username:
        return f"@{participant.username}"
    elif participant.first_name:
        return participant.first_name
    return f"User {participant.user_id}"


# ─── Handler Registration ────────────────────────────────────────────────────────


def get_giveaway_handlers() -> list:
    """Return all giveaway-related handlers."""
    create_conv = ConversationHandler(
        entry_points=[CommandHandler("newgiveaway", new_giveaway_start)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, giveaway_title)],
            DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, giveaway_description),
                CommandHandler("skip", giveaway_description),
            ],
            PRIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, giveaway_prize)],
            WINNER_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, giveaway_winner_count)],
            DURATION: [CallbackQueryHandler(giveaway_duration, pattern=r"^dur_")],
        },
        fallbacks=[CommandHandler("cancel", cancel_creation)],
    )

    return [
        create_conv,
        CommandHandler("draw", draw_giveaway),
        CommandHandler("mygiveaways", my_giveaways),
        CommandHandler("cancelgiveaway", cancel_giveaway),
        CallbackQueryHandler(join_giveaway_callback, pattern=r"^join_gw_\d+$"),
    ]
