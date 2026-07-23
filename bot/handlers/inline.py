"""Inline mode: lets creators push a giveaway/contest post into any chat.

The "📢 Kanalga/Guruhga yuborish" share button uses switch_inline_query_chosen_chat,
which opens a chat picker and sends an inline query like "gw_12" to the bot.
This handler answers it with the giveaway post (media + join button), so picking
the result publishes the post in the chosen chat.

NOTE: Inline mode must be enabled for the bot in @BotFather (/setinline).
"""

import logging

from sqlalchemy import select
from telegram import (
    InlineQueryResultArticle,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedMpeg4Gif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InputTextMessageContent,
    Update,
)
from telegram.ext import ContextTypes, InlineQueryHandler

from bot.handlers.giveaway import _join_button
from bot.models import Contest, Giveaway, async_session
from bot.utils.lang import get_user_lang

logger = logging.getLogger(__name__)


def _giveaway_result(gw, lang: str):
    """Build an inline query result carrying the giveaway post + join button."""
    kb = _join_button(gw.id, 0, lang, custom_label=gw.button_label)
    caption = gw.post_text or gw.title
    rid = f"gw_{gw.id}"

    if gw.post_file_id and gw.post_media_type == "photo":
        return InlineQueryResultCachedPhoto(
            id=rid, photo_file_id=gw.post_file_id,
            caption=caption, parse_mode="HTML", reply_markup=kb,
        )
    if gw.post_file_id and gw.post_media_type == "video":
        return InlineQueryResultCachedVideo(
            id=rid, video_file_id=gw.post_file_id, title=gw.title,
            caption=caption, parse_mode="HTML", reply_markup=kb,
        )
    if gw.post_file_id and gw.post_media_type == "animation":
        return InlineQueryResultCachedMpeg4Gif(
            id=rid, mpeg4_file_id=gw.post_file_id,
            caption=caption, parse_mode="HTML", reply_markup=kb,
        )
    if gw.post_file_id and gw.post_media_type == "document":
        return InlineQueryResultCachedDocument(
            id=rid, document_file_id=gw.post_file_id, title=gw.title,
            caption=caption, parse_mode="HTML", reply_markup=kb,
        )
    return InlineQueryResultArticle(
        id=rid, title=f"🎁 {gw.title}",
        description="Yutuqli o'yin postini yuborish",
        input_message_content=InputTextMessageContent(caption, parse_mode="HTML"),
        reply_markup=kb,
    )


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer inline queries: 'gw_<id>' / 'ct_<id>' or a list of own games."""
    query = update.inline_query
    text = (query.query or "").strip()
    user_id = query.from_user.id
    lang = await get_user_lang(user_id)

    results = []
    async with async_session() as session:
        if text.startswith("gw_") and text[3:].isdigit():
            gw = (await session.execute(
                select(Giveaway).where(Giveaway.id == int(text[3:]))
            )).scalar_one_or_none()
            if gw and gw.status in ("queued", "active") and gw.creator_id == user_id:
                results.append(_giveaway_result(gw, lang))
        elif text.startswith("ct_") and text[3:].isdigit():
            ct = (await session.execute(
                select(Contest).where(Contest.id == int(text[3:]))
            )).scalar_one_or_none()
            if ct and ct.creator_id == user_id:
                body = ct.post_text or f"🏅 <b>{ct.title}</b>\n\n{ct.description or ''}"
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📤 Qatnashish", callback_data=f"submit_contest_{ct.id}")
                ]])
                results.append(InlineQueryResultArticle(
                    id=f"ct_{ct.id}", title=f"🏅 {ct.title}",
                    description="Konkurs postini yuborish",
                    input_message_content=InputTextMessageContent(body, parse_mode="HTML"),
                    reply_markup=kb,
                ))
        else:
            # No specific query — offer the user's own shareable giveaways
            rows = (await session.execute(
                select(Giveaway).where(
                    Giveaway.creator_id == user_id,
                    Giveaway.status.in_(["queued", "active"]),
                ).order_by(Giveaway.created_at.desc()).limit(10)
            )).scalars().all()
            for gw in rows:
                results.append(_giveaway_result(gw, lang))

    try:
        await query.answer(results, cache_time=5, is_personal=True)
    except Exception as e:
        logger.warning("Inline query answer failed: %s", e)


def get_inline_handlers() -> list:
    return [InlineQueryHandler(inline_query_handler)]
