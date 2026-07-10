"""Giveaway command handlers — post-based creation flow.

Admins send their giveaway post (text/photo/video) as-is.
Bot asks only: winner count, duration, required channels.
Then publishes the original post with a Join button.
"""

import random
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.config import settings
from bot.i18n import get_text
from bot.models import (
    Giveaway,
    GiveawayParticipant,
    GiveawayStatus,
    GiveawayWinner,
    async_session,
)
from bot.utils.lang import get_user_lang, t
from bot.utils.referral import verify_pending_referrals
from bot.utils.subscription import (
    build_subscription_keyboard,
    get_unsubscribed,
    parse_channels,
    serialize_channels,
)

# Conversation states (post-based flow)
POST, WINNERS, DURATION, CHANNELS = range(4)


def _join_button(giveaway_id: int, participant_count: int, lang: str) -> InlineKeyboardMarkup:
    """Build the 'Join' button for a giveaway announcement."""
    label = f"🎮 {get_text('gw_join_button', lang=lang)} ({participant_count})"
    web_url = settings.WEB_URL
    if web_url:
        url = f"{web_url.rstrip('/')}/miniapp/giveaway?id={giveaway_id}"
        button = InlineKeyboardButton(label, web_app=WebAppInfo(url=url))
    else:
        button = InlineKeyboardButton(label, callback_data=f"join_gw_{giveaway_id}")
    return InlineKeyboardMarkup([[button]])


def _share_keyboard(game_type: str, game_id: int, lang: str) -> InlineKeyboardMarkup:
    """Build share/forward buttons shown to creator after game creation."""
    bot_username = settings.BOT_USERNAME or "qurachibot"
    deep_link = f"https://t.me/{bot_username}?start={game_type}_{game_id}"

    labels = {
        "uz": ("📢 Kanalga/Guruhga yuborish", "🔗 Havolani nusxalash"),
        "ru": ("📢 Отправить в канал/группу", "🔗 Скопировать ссылку"),
        "en": ("📢 Send to channel/group", "🔗 Copy link"),
    }
    share_label, link_label = labels.get(lang, labels["uz"])

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(share_label, switch_inline_query_chosen_chat=f"{game_type}_{game_id}")],
        [InlineKeyboardButton(link_label, url=deep_link)],
    ])


def _extract_post_data(message) -> dict:
    """Extract text, file_id, and media_type from a Telegram message."""
    data = {"post_text": None, "post_file_id": None, "post_media_type": None}

    if message.photo:
        data["post_file_id"] = message.photo[-1].file_id
        data["post_media_type"] = "photo"
        data["post_text"] = message.caption_html or message.caption or ""
    elif message.video:
        data["post_file_id"] = message.video.file_id
        data["post_media_type"] = "video"
        data["post_text"] = message.caption_html or message.caption or ""
    elif message.animation:
        data["post_file_id"] = message.animation.file_id
        data["post_media_type"] = "animation"
        data["post_text"] = message.caption_html or message.caption or ""
    elif message.document:
        data["post_file_id"] = message.document.file_id
        data["post_media_type"] = "document"
        data["post_text"] = message.caption_html or message.caption or ""
    else:
        data["post_text"] = message.text_html or message.text or ""

    return data


async def send_giveaway_post(bot, chat_id, giveaway, keyboard):
    """Re-send the admin's original post with the join button attached."""
    if giveaway.post_file_id and giveaway.post_media_type:
        media_type = giveaway.post_media_type
        caption = giveaway.post_text or ""
        if media_type == "photo":
            return await bot.send_photo(
                chat_id, giveaway.post_file_id,
                caption=caption, parse_mode="HTML", reply_markup=keyboard,
            )
        elif media_type == "video":
            return await bot.send_video(
                chat_id, giveaway.post_file_id,
                caption=caption, parse_mode="HTML", reply_markup=keyboard,
            )
        elif media_type == "animation":
            return await bot.send_animation(
                chat_id, giveaway.post_file_id,
                caption=caption, parse_mode="HTML", reply_markup=keyboard,
            )
        elif media_type == "document":
            return await bot.send_document(
                chat_id, giveaway.post_file_id,
                caption=caption, parse_mode="HTML", reply_markup=keyboard,
            )
    # Text-only
    return await bot.send_message(
        chat_id, giveaway.post_text or giveaway.title,
        parse_mode="HTML", reply_markup=keyboard,
    )


# ─── Create Giveaway (post-based) ────────────────────────────────────────────


async def new_giveaway_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start giveaway creation. Ask admin to send the post."""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    context.user_data["lang"] = lang
    await update.message.reply_text(
        get_text("gw_send_post", lang=lang), parse_mode="HTML"
    )
    return POST


async def giveaway_receive_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the admin's giveaway post (text/photo/video/etc)."""
    lang = context.user_data.get("lang", "en")
    message = update.message

    # Extract post content
    post_data = _extract_post_data(message)
    context.user_data["post_data"] = post_data

    # Auto-generate a title from first line of text (for internal reference)
    text_content = post_data["post_text"] or ""
    # Strip HTML tags for title extraction
    import re
    plain_text = re.sub(r"<[^>]+>", "", text_content)
    first_line = plain_text.strip().split("\n")[0][:100] if plain_text.strip() else "Giveaway"
    context.user_data["title"] = first_line

    # Ask winner count
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1", callback_data="gwwin_1"),
            InlineKeyboardButton("2", callback_data="gwwin_2"),
            InlineKeyboardButton("3", callback_data="gwwin_3"),
        ],
        [
            InlineKeyboardButton("5", callback_data="gwwin_5"),
            InlineKeyboardButton("10", callback_data="gwwin_10"),
        ],
    ])
    await message.reply_text(
        get_text("gw_ask_winners", lang=lang), reply_markup=keyboard, parse_mode="HTML"
    )
    return WINNERS


async def giveaway_winners_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle winner count button press."""
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")

    count = int(query.data.split("_")[1])
    context.user_data["winner_count"] = count

    # Ask duration
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(get_text("dur_1h", lang=lang), callback_data="gwdur_1h"),
            InlineKeyboardButton(get_text("dur_6h", lang=lang), callback_data="gwdur_6h"),
            InlineKeyboardButton(get_text("dur_12h", lang=lang), callback_data="gwdur_12h"),
        ],
        [
            InlineKeyboardButton(get_text("dur_24h", lang=lang), callback_data="gwdur_24h"),
            InlineKeyboardButton(get_text("dur_3d", lang=lang), callback_data="gwdur_3d"),
            InlineKeyboardButton(get_text("dur_7d", lang=lang), callback_data="gwdur_7d"),
        ],
        [InlineKeyboardButton(get_text("dur_none", lang=lang), callback_data="gwdur_none")],
    ])
    await query.edit_message_text(
        get_text("gw_ask_duration", lang=lang), reply_markup=keyboard, parse_mode="HTML"
    )
    return DURATION


async def giveaway_duration_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle duration button press."""
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")

    duration_map = {
        "gwdur_1h": timedelta(hours=1),
        "gwdur_6h": timedelta(hours=6),
        "gwdur_12h": timedelta(hours=12),
        "gwdur_24h": timedelta(hours=24),
        "gwdur_3d": timedelta(days=3),
        "gwdur_7d": timedelta(days=7),
        "gwdur_none": None,
    }
    duration = duration_map.get(query.data)
    context.user_data["ends_at"] = (datetime.utcnow() + duration) if duration else None

    # Ask channels
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(get_text("gw_add_channels_btn", lang=lang), callback_data="gwch_add"),
            InlineKeyboardButton(get_text("gw_skip_channels_btn", lang=lang), callback_data="gwch_skip"),
        ],
    ])
    await query.edit_message_text(
        get_text("gw_ask_channels", lang=lang), reply_markup=keyboard, parse_mode="HTML"
    )
    return CHANNELS


async def giveaway_channels_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle channels skip/add choice."""
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")

    if query.data == "gwch_skip":
        context.user_data["channels"] = None
        return await _finalize_giveaway(query, context)
    else:
        # Ask user to type channels
        await query.edit_message_text(
            get_text("gw_type_channels", lang=lang), parse_mode="HTML"
        )
        return CHANNELS


async def giveaway_channels_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive typed channels and finalize."""
    lang = context.user_data.get("lang", "en")
    text = update.message.text.strip()
    channels = parse_channels(text)
    context.user_data["channels"] = serialize_channels(channels) if channels else None

    # Finalize — we don't have a callback_query here, so send a new message
    return await _finalize_giveaway_from_message(update, context)


async def _finalize_giveaway(query, context) -> int:
    """Save giveaway to DB and publish the admin's original post."""
    lang = context.user_data.get("lang", "en")
    post_data = context.user_data["post_data"]

    async with async_session() as session:
        giveaway = Giveaway(
            title=context.user_data["title"],
            post_text=post_data["post_text"],
            post_file_id=post_data["post_file_id"],
            post_media_type=post_data["post_media_type"],
            winner_count=context.user_data["winner_count"],
            required_channels=context.user_data.get("channels"),
            creator_id=query.from_user.id,
            creator_username=query.from_user.username,
            chat_id=query.message.chat_id,
            ends_at=context.user_data.get("ends_at"),
        )
        session.add(giveaway)
        await session.commit()
        await session.refresh(giveaway)

    join_keyboard = _join_button(giveaway.id, 0, lang)

    # Delete the settings message
    try:
        await query.delete_message()
    except Exception:
        pass

    # Send the admin's original post with Join button
    await send_giveaway_post(context.bot, query.message.chat_id, giveaway, join_keyboard)

    # Send share buttons to the creator
    share_kb = _share_keyboard("gw", giveaway.id, lang)
    share_texts = {
        "uz": "✅ <b>Yutuqli o'yin yaratildi!</b>\n\nEndi uni kanalingizga yuboring:",
        "ru": "✅ <b>Розыгрыш создан!</b>\n\nТеперь отправьте его в канал:",
        "en": "✅ <b>Giveaway created!</b>\n\nNow share it to your channel:",
    }
    await context.bot.send_message(
        query.message.chat_id, share_texts.get(lang, share_texts["uz"]),
        reply_markup=share_kb, parse_mode="HTML",
    )

    context.user_data.clear()
    return ConversationHandler.END


async def _finalize_giveaway_from_message(update: Update, context) -> int:
    """Same as _finalize_giveaway but triggered from a text message (channels typed)."""
    lang = context.user_data.get("lang", "en")
    post_data = context.user_data["post_data"]

    async with async_session() as session:
        giveaway = Giveaway(
            title=context.user_data["title"],
            post_text=post_data["post_text"],
            post_file_id=post_data["post_file_id"],
            post_media_type=post_data["post_media_type"],
            winner_count=context.user_data["winner_count"],
            required_channels=context.user_data.get("channels"),
            creator_id=update.effective_user.id,
            creator_username=update.effective_user.username,
            chat_id=update.effective_chat.id,
            ends_at=context.user_data.get("ends_at"),
        )
        session.add(giveaway)
        await session.commit()
        await session.refresh(giveaway)

    join_keyboard = _join_button(giveaway.id, 0, lang)

    # Send the admin's original post with Join button
    await send_giveaway_post(context.bot, update.effective_chat.id, giveaway, join_keyboard)

    # Send share buttons to the creator
    share_kb = _share_keyboard("gw", giveaway.id, lang)
    share_texts = {
        "uz": "✅ <b>Yutuqli o'yin yaratildi!</b>\n\nEndi uni kanalingizga yuboring:",
        "ru": "✅ <b>Розыгрыш создан!</b>\n\nТеперь отправьте его в канал:",
        "en": "✅ <b>Giveaway created!</b>\n\nNow share it to your channel:",
    }
    await update.message.reply_text(
        share_texts.get(lang, share_texts["uz"]),
        reply_markup=share_kb, parse_mode="HTML",
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
    """Handle 'Join Giveaway' button press."""
    query = update.callback_query
    giveaway_id = int(query.data.split("_")[-1])
    user = query.from_user
    lang = await get_user_lang(user.id)

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).where(Giveaway.id == giveaway_id)
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

        required_channels = parse_channels(giveaway.required_channels)

    # Enforce channel subscription
    if required_channels:
        missing = await get_unsubscribed(context.bot, user.id, required_channels)
        if missing:
            await query.answer(get_text("gw_must_subscribe", lang=lang), show_alert=True)
            keyboard = build_subscription_keyboard(
                missing,
                retry_callback=f"join_gw_{giveaway_id}",
                check_label=get_text("gw_check_subscription", lang=lang),
            )
            try:
                await context.bot.send_message(
                    user.id,
                    get_text("gw_subscribe_prompt", lang=lang, title=giveaway.title),
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

    async with async_session() as session:
        participant = GiveawayParticipant(
            giveaway_id=giveaway_id,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
        )
        session.add(participant)
        await session.commit()

        participant_count = (
            await session.execute(
                select(func.count(GiveawayParticipant.id)).where(
                    GiveawayParticipant.giveaway_id == giveaway_id
                )
            )
        ).scalar()

    await verify_pending_referrals(context.bot, user.id)
    await query.answer(get_text("gw_joined", lang=lang, count=participant_count))

    # Update inline keyboard with new participant count
    join_keyboard = _join_button(giveaway_id, participant_count, lang)
    try:
        await query.edit_message_reply_markup(reply_markup=join_keyboard)
    except Exception:
        pass


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
        prize=giveaway.prize or "",
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
            f"   👤 {len(gw.participants)} | {gw.status.value}\n\n"
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
            POST: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL)
                    & ~filters.COMMAND,
                    giveaway_receive_post,
                ),
            ],
            WINNERS: [CallbackQueryHandler(giveaway_winners_selected, pattern=r"^gwwin_")],
            DURATION: [CallbackQueryHandler(giveaway_duration_selected, pattern=r"^gwdur_")],
            CHANNELS: [
                CallbackQueryHandler(giveaway_channels_choice, pattern=r"^gwch_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, giveaway_channels_typed),
            ],
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
