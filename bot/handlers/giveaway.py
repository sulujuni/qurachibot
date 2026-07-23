"""Giveaway command handlers — full creation flow with scheduling.

Flow: POST → PREVIEW → CHANNEL (validated) → WINNERS → START_TIME → END_TIME → SUB_CHANNELS → FINALIZE
Supports scheduled publishing (QUEUED state) and immediate publish (ACTIVE).
"""

import logging
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
    GiveawayPost,
    GiveawayStatus,
    GiveawayWinner,
    async_session,
)
from bot.models.user_channel import UserChannel
from bot.utils.lang import get_user_lang, t
from bot.utils.referral import verify_pending_referrals
from bot.utils.timeutil import fmt_local, parse_user_datetime
from bot.utils.subscription import (
    build_subscription_keyboard,
    get_unsubscribed,
    parse_channels,
    serialize_channels,
)

logger = logging.getLogger(__name__)

# Conversation states (full creation flow with scheduling + channel validation)
POST, PREVIEW, BUTTON_LABEL, CHANNEL, WINNERS, START_TIME, END_TIME, SUB_CHANNELS, BOOST_CHANNELS = range(9)


def _join_button(giveaway_id: int, participant_count: int, lang: str, custom_label: str | None = None, use_webapp: bool = False) -> InlineKeyboardMarkup:
    """Build the 'Join' button for a giveaway announcement.

    Channel/group posts can't use web_app inline buttons (Button_type_invalid
    outside private chats). Preference order:
    1. use_webapp=True (private chat) → web_app button
    2. MINIAPP_SHORT_NAME configured → direct-link Mini App URL button, which
       opens the animated join screen from any chat, including channels
    3. plain callback button (join happens in place)
    """
    if custom_label:
        label = f"{custom_label} ({participant_count})"
    else:
        label = f"🎮 {get_text('gw_join_button', lang=lang)} ({participant_count})"
    web_url = settings.WEB_URL
    if use_webapp and web_url:
        url = f"{web_url.rstrip('/')}/miniapp/giveaway?id={giveaway_id}"
        button = InlineKeyboardButton(label, web_app=WebAppInfo(url=url))
    elif settings.MINIAPP_SHORT_NAME and settings.BOT_USERNAME:
        url = f"https://t.me/{settings.BOT_USERNAME}/{settings.MINIAPP_SHORT_NAME}?startapp=gw_{giveaway_id}"
        button = InlineKeyboardButton(label, url=url)
    else:
        button = InlineKeyboardButton(label, callback_data=f"join_gw_{giveaway_id}")
    return InlineKeyboardMarkup([[button]])


def render_owner_template(template: str, *, title: str, winners_text: str = "", count: int = 0, prize: str = "") -> str:
    """Fill an owner-provided message template.

    Supported placeholders: {winners} {title} {count} {prize} (plus Uzbek
    aliases {goliblar} {ishtirokchilar}). Plain str.replace so stray braces
    in the owner's text can't crash formatting.
    """
    return (
        template
        .replace("{winners}", winners_text)
        .replace("{goliblar}", winners_text)
        .replace("{title}", title)
        .replace("{count}", str(count))
        .replace("{ishtirokchilar}", str(count))
        .replace("{prize}", prize or "")
    )


async def get_published_posts(giveaway) -> list[tuple[int, int]]:
    """All (chat_id, message_id) pairs where this giveaway's post was published."""
    async with async_session() as session:
        rows = (await session.execute(
            select(GiveawayPost).where(GiveawayPost.giveaway_id == giveaway.id)
        )).scalars().all()
    posts = [(p.chat_id, p.message_id) for p in rows]
    # Legacy fallback: giveaways published before the posts table existed
    if not posts and giveaway.channel_id and giveaway.message_id:
        posts = [(giveaway.channel_id, giveaway.message_id)]
    return posts


def _target_channel_ids(giveaway) -> list[int]:
    """Channels the giveaway should be published to."""
    ids: list[int] = []
    if giveaway.target_channels:
        for tok in giveaway.target_channels.split(","):
            tok = tok.strip()
            if tok.lstrip("-").isdigit():
                ids.append(int(tok))
    if not ids and giveaway.channel_id:
        ids = [giveaway.channel_id]
    return ids


async def publish_giveaway_to_channels(bot, giveaway, lang: str):
    """Publish the giveaway post to every target channel.

    Returns (posts, errors): posts = [(chat_id, message_id)], errors =
    [(channel, exception)]. Persists GiveawayPost rows and sets the primary
    channel_id/message_id/published_at on success.
    """
    targets = _target_channel_ids(giveaway)
    kb = _join_button(giveaway.id, 0, lang, custom_label=giveaway.button_label)

    posts: list[tuple[int, int]] = []
    errors: list[tuple[int, Exception]] = []
    for ch in targets:
        try:
            msg = await send_giveaway_post(bot, ch, giveaway, kb)
            posts.append((msg.chat.id, msg.message_id))
        except Exception as e:
            logger.error(f"Publish giveaway {giveaway.id} to {ch} failed: {e}")
            errors.append((ch, e))

    if posts:
        async with async_session() as session:
            g = (await session.execute(select(Giveaway).where(Giveaway.id == giveaway.id))).scalar_one()
            g.channel_id, g.message_id = posts[0]
            g.published_at = datetime.utcnow()
            for chat_id, message_id in posts:
                session.add(GiveawayPost(giveaway_id=giveaway.id, chat_id=chat_id, message_id=message_id))
            await session.commit()
    return posts, errors


# Last participant count pushed to each giveaway's channel buttons. Lets the
# once-per-minute refresh job skip giveaways whose count hasn't changed, so we
# never re-edit an unchanged post. Freshly published posts show "(0)", so a
# missing entry is treated as 0.
_last_pushed_counts: dict[int, int] = {}


async def update_all_post_counters(bot, giveaway_id: int) -> None:
    """Refresh the join-button counter on every published copy of the post.

    No-op when the count is unchanged since the last push, so this is safe to
    call for every active giveaway once a minute without spamming edits.
    """
    async with async_session() as session:
        gw = (await session.execute(select(Giveaway).where(Giveaway.id == giveaway_id))).scalar_one_or_none()
        if not gw:
            return
        count = (await session.execute(
            select(func.count(GiveawayParticipant.id)).where(GiveawayParticipant.giveaway_id == giveaway_id)
        )).scalar() or 0

    if _last_pushed_counts.get(giveaway_id, 0) == count:
        return  # displayed count already current — skip the edit

    posts = await get_published_posts(gw)
    if not posts:
        _last_pushed_counts[giveaway_id] = count
        return

    lang = await get_user_lang(gw.creator_id)
    kb = _join_button(giveaway_id, count, lang, custom_label=gw.button_label)
    for chat_id, message_id in posts:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=kb)
        except Exception:
            pass  # "message is not modified", deleted post, etc.
    _last_pushed_counts[giveaway_id] = count


async def _weighted_draw(participants, winner_count: int, boost_channels_str: str | None, bot) -> list:
    """Draw winners weighted by boost-channel subscriptions.
    Weight = 1 + number of verified boost subscriptions per participant.
    """
    if not boost_channels_str or not boost_channels_str.strip():
        return random.sample(list(participants), min(winner_count, len(participants)))

    boost_list = [ch.strip() for ch in boost_channels_str.split(",") if ch.strip()]
    if not boost_list:
        return random.sample(list(participants), min(winner_count, len(participants)))

    # Calculate weights
    weights = []
    for p in participants:
        verified = 0
        for ch in boost_list:
            try:
                member = await bot.get_chat_member(chat_id=ch, user_id=p.user_id)
                if member.status not in ("left", "kicked"):
                    verified += 1
            except Exception:
                pass
        weights.append(1 + verified)

    # Weighted selection without replacement
    p_list = list(participants)
    selected = []
    remaining_idx = list(range(len(p_list)))
    remaining_w = list(weights)
    for _ in range(min(winner_count, len(p_list))):
        if not remaining_idx:
            break
        chosen = random.choices(remaining_idx, weights=remaining_w, k=1)[0]
        idx = remaining_idx.index(chosen)
        selected.append(p_list[chosen])
        remaining_idx.pop(idx)
        remaining_w.pop(idx)
    return selected


def _share_keyboard(game_type: str, game_id: int, lang: str) -> InlineKeyboardMarkup:
    """Build share/forward buttons shown to creator after game creation."""
    from telegram import SwitchInlineQueryChosenChat

    bot_username = settings.BOT_USERNAME or "qurachibot"
    deep_link = f"https://t.me/{bot_username}?start={game_type}_{game_id}"

    labels = {
        "uz": ("📢 Kanalga/Guruhga yuborish", "🔗 Havolani nusxalash"),
        "ru": ("📢 Отправить в канал/группу", "🔗 Скопировать ссылку"),
        "en": ("📢 Send to channel/group", "🔗 Copy link"),
    }
    share_label, link_label = labels.get(lang, labels["uz"])

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(share_label, switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
            query=f"{game_type}_{game_id}",
            allow_user_chats=True, allow_group_chats=True, allow_channel_chats=True,
        ))],
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


async def close_published_post(bot, giveaway) -> None:
    """Remove the join button from every published copy of the post after the draw."""
    _last_pushed_counts.pop(giveaway.id, None)  # stop tracking a finished giveaway
    for chat_id, message_id in await get_published_posts(giveaway):
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None,
            )
        except Exception:
            pass


async def announce_results(bot, giveaway, winners, total_participants: int) -> None:
    """Announce winners in every channel (as replies to the posts) and DM them.

    Uses the owner's winner_template when set ({winners} {title} {count}
    {prize} placeholders), otherwise the built-in text.
    """
    mention_text = "\n".join(
        f'🏆 {i+1}. <a href="tg://user?id={w.user_id}">{w.first_name or w.username or "User"}</a>'
        for i, w in enumerate(winners)
    )
    if giveaway.winner_template and giveaway.winner_template.strip():
        announce_text = render_owner_template(
            giveaway.winner_template,
            title=giveaway.title, winners_text=mention_text,
            count=total_participants, prize=giveaway.prize,
        )
    else:
        announce_text = (
            f"🎊 <b>{giveaway.title}</b>\n\n"
            f"👤 Ishtirokchilar: {total_participants}\n\n"
            f"<b>🏆 G'oliblar:</b>\n{mention_text}\n\n"
            f"Tabriklaymiz! 🎉"
        )

    posts = await get_published_posts(giveaway)
    if posts:
        for chat_id, message_id in posts:
            try:
                await bot.send_message(
                    chat_id, announce_text, parse_mode="HTML",
                    reply_to_message_id=message_id,
                )
            except Exception as e:
                logger.warning(f"Could not announce giveaway {giveaway.id} in {chat_id}: {e}")
    else:
        # Never published to a channel — announce in the creation chat
        try:
            await bot.send_message(giveaway.chat_id, announce_text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Could not announce giveaway {giveaway.id}: {e}")

    await close_published_post(bot, giveaway)

    for w in winners:
        try:
            w_lang = await get_user_lang(w.user_id)
            win_msgs = {
                "uz": f"🎉 <b>Tabriklaymiz!</b>\n\n<b>{giveaway.title}</b> o'yinida g'olib bo'ldingiz!\n🎁 {giveaway.prize or ''}\n\nTashkilotchi bilan bog'laning!",
                "ru": f"🎉 <b>Поздравляем!</b>\n\nВы победили: <b>{giveaway.title}</b>!\n🎁 {giveaway.prize or ''}",
                "en": f"🎉 <b>Congratulations!</b>\n\nYou won: <b>{giveaway.title}</b>!\n🎁 {giveaway.prize or ''}",
            }
            await bot.send_message(w.user_id, win_msgs.get(w_lang, win_msgs["uz"]), parse_mode="HTML")
        except Exception:
            pass  # User may have blocked the bot


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
    """Receive the admin's giveaway post (text/photo/video/etc) and show preview."""
    lang = context.user_data.get("lang", "en")
    message = update.message

    # Extract post content
    post_data = _extract_post_data(message)
    context.user_data["post_data"] = post_data

    # Auto-generate a title from first line of text (for internal reference)
    text_content = post_data["post_text"] or ""
    import re
    plain_text = re.sub(r"<[^>]+>", "", text_content)
    first_line = plain_text.strip().split("\n")[0][:100] if plain_text.strip() else "Giveaway"
    context.user_data["title"] = first_line

    # Show preview — re-send the post as it will appear
    preview_labels = {
        "uz": "👁 <b>Ko'rib chiqish:</b>\nPost shunday ko'rinadi. Tasdiqlaysizmi?",
        "ru": "👁 <b>Предпросмотр:</b>\nВот как будет выглядеть пост. Подтвердить?",
        "en": "👁 <b>Preview:</b>\nThis is how the post will look. Confirm?",
    }
    await message.reply_text(preview_labels.get(lang, preview_labels["uz"]), parse_mode="HTML")

    # Re-send the content as preview
    if post_data["post_file_id"] and post_data["post_media_type"]:
        mt = post_data["post_media_type"]
        caption = post_data["post_text"] or ""
        if mt == "photo":
            await context.bot.send_photo(message.chat_id, post_data["post_file_id"], caption=caption, parse_mode="HTML")
        elif mt == "video":
            await context.bot.send_video(message.chat_id, post_data["post_file_id"], caption=caption, parse_mode="HTML")
        elif mt == "animation":
            await context.bot.send_animation(message.chat_id, post_data["post_file_id"], caption=caption, parse_mode="HTML")
        else:
            await context.bot.send_document(message.chat_id, post_data["post_file_id"], caption=caption, parse_mode="HTML")
    else:
        await message.reply_text(post_data["post_text"] or "—", parse_mode="HTML")

    # Confirm/redo buttons
    confirm_labels = {
        "uz": ("✅ Tasdiqlash", "🔄 Qaytadan yuborish"),
        "ru": ("✅ Подтвердить", "🔄 Отправить заново"),
        "en": ("✅ Confirm", "🔄 Send again"),
    }
    cl, rl = confirm_labels.get(lang, confirm_labels["uz"])
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(cl, callback_data="gwprev_confirm")],
        [InlineKeyboardButton(rl, callback_data="gwprev_redo")],
    ])
    await message.reply_text(
        "👆", reply_markup=keyboard,
    )
    return PREVIEW


async def giveaway_preview_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle preview confirm/redo → ask for channel."""
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")

    if query.data == "gwprev_redo":
        await query.edit_message_text(get_text("gw_send_post", lang=lang), parse_mode="HTML")
        return POST

    # Confirmed — ask for button label
    presets = {
        "uz": ["🎁 Qatnashish!", "Qatnashish", "💎 Qo'shilish", "🍀 Men ham!"],
        "ru": ["🎁 Участвовать!", "Участвовать", "💎 Вступить", "🍀 Я в деле!"],
        "en": ["🎁 Participate!", "Participate", "💎 Join", "🍀 I'm in!"],
    }
    preset_list = presets.get(lang, presets["uz"])
    buttons = [[InlineKeyboardButton(p, callback_data=f"gwbtn_{i}")] for i, p in enumerate(preset_list)]
    buttons.append([InlineKeyboardButton(get_text("gw_btn_custom", lang=lang), callback_data="gwbtn_custom")])
    buttons.append([InlineKeyboardButton(get_text("gw_btn_default", lang=lang), callback_data="gwbtn_default")])
    context.user_data["btn_presets"] = preset_list

    await query.edit_message_text(get_text("gw_ask_button_label", lang=lang), reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
    return BUTTON_LABEL


async def giveaway_button_label_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle button label selection → ask for channel."""
    lang = context.user_data.get("lang", "en")
    channel_prompts = {
        "uz": "📢 <b>Kanal tanlang</b>\n\nO'yin joylanadigan kanal @username yoki ID sini yuboring.\n\n💡 Yopiq kanallar uchun: @idbot ga ulashing.\n⚠️ Siz va bot kanalda admin bo'lishi kerak.",
        "ru": "📢 <b>Выберите канал</b>\n\nОтправьте @username или ID.\n⚠️ Вы и бот должны быть админами.",
        "en": "📢 <b>Select channel</b>\n\nSend @username or ID.\n⚠️ You and bot must be admins.",
    }

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "gwbtn_default":
            context.user_data["button_label"] = None
        elif query.data == "gwbtn_custom":
            await query.edit_message_text(get_text("gw_type_button_label", lang=lang), parse_mode="HTML")
            return BUTTON_LABEL
        else:
            idx = int(query.data.split("_")[1])
            presets = context.user_data.get("btn_presets", [])
            context.user_data["button_label"] = presets[idx] if idx < len(presets) else None
        await query.edit_message_text(channel_prompts.get(lang, channel_prompts["uz"]), parse_mode="HTML")
        return CHANNEL
    else:
        # Custom text typed
        context.user_data["button_label"] = update.message.text.strip()[:100]
        await update.message.reply_text(channel_prompts.get(lang, channel_prompts["uz"]), parse_mode="HTML")
        return CHANNEL


async def giveaway_channel_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate channel: user is admin + bot can post (test-send-delete)."""
    lang = context.user_data.get("lang", "en")
    message = update.message
    channel_input = message.text.strip()

    # Parse channel ID
    channel_id = int(channel_input) if channel_input.lstrip("-").isdigit() else channel_input

    # 1. Check user is admin
    try:
        member = await context.bot.get_chat_member(chat_id=channel_id, user_id=message.from_user.id)
        if member.status not in ("creator", "administrator"):
            await message.reply_text("❌ Siz bu kanalning admini emassiz." if lang == "uz" else "❌ You're not an admin.")
            return CHANNEL
    except Exception:
        await message.reply_text("❌ Kanal topilmadi yoki bot qo'shilmagan." if lang == "uz" else "❌ Channel not found or bot not added.")
        return CHANNEL

    # 2. Test-send-delete
    try:
        test_msg = await context.bot.send_message(channel_id, "🔧 test")
        await context.bot.delete_message(channel_id, test_msg.message_id)
    except Exception:
        await message.reply_text("❌ Bot yoza olmaydi. Botga 'Post xabarlar' ruxsatini bering." if lang == "uz" else "❌ Bot can't post. Give it Post Messages permission.")
        return CHANNEL

    # Save channel info (a giveaway can run on several channels at once)
    try:
        chat_info = await context.bot.get_chat(channel_id)
        ch_id, ch_title = chat_info.id, chat_info.title
    except Exception:
        ch_id, ch_title = channel_id, str(channel_id)

    channels = context.user_data.setdefault("channels_list", [])
    if all(c[0] != ch_id for c in channels):
        channels.append((ch_id, ch_title))
    # Primary channel = first added
    context.user_data["channel_id"] = channels[0][0]
    context.user_data["channel_title"] = ", ".join(c[1] for c in channels)

    names = "\n".join(f"• {c[1]}" for c in channels)
    more_labels = {
        "uz": (f"✅ Kanallar:\n{names}\n\nYana kanal qo'shasizmi? O'yin barcha kanallarga bir vaqtda joylanadi.", "➕ Yana kanal", "➡️ Davom etish"),
        "ru": (f"✅ Каналы:\n{names}\n\nДобавить ещё канал? Розыгрыш публикуется во все каналы одновременно.", "➕ Ещё канал", "➡️ Продолжить"),
        "en": (f"✅ Channels:\n{names}\n\nAdd another channel? The giveaway is posted to all of them.", "➕ Add channel", "➡️ Continue"),
    }
    txt, more_lbl, next_lbl = more_labels.get(lang, more_labels["uz"])
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(more_lbl, callback_data="gwch_more"),
         InlineKeyboardButton(next_lbl, callback_data="gwch_next")],
    ])
    await message.reply_text(txt, reply_markup=keyboard)
    return CHANNEL


async def giveaway_channel_more_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle '➕ Add channel' / '➡️ Continue' buttons in the channel step."""
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")

    if query.data == "gwch_more":
        await query.edit_message_text(
            "📢 Keyingi kanal @username yoki ID sini yuboring:" if lang == "uz"
            else "📢 Отправьте @username или ID следующего канала:" if lang == "ru"
            else "📢 Send the next channel @username or ID:"
        )
        return CHANNEL

    # gwch_next → ask winner count
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1", callback_data="gwwin_1"), InlineKeyboardButton("2", callback_data="gwwin_2"), InlineKeyboardButton("3", callback_data="gwwin_3")],
        [InlineKeyboardButton("5", callback_data="gwwin_5"), InlineKeyboardButton("10", callback_data="gwwin_10")],
    ])
    await query.edit_message_text(get_text("gw_ask_winners", lang=lang), reply_markup=keyboard, parse_mode="HTML")
    return WINNERS


async def giveaway_winners_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle winner count → ask start time."""
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")
    context.user_data["winner_count"] = int(query.data.split("_")[1])

    # Ask start time
    prompt = "⏳ <b>Boshlanish vaqti</b> (Toshkent vaqti)\n\nHozir yoki sana kiriting: <code>2025-12-31 21:00</code>" if lang == "uz" else "⏳ <b>Start time</b> (local time)\n\nNow or enter date: <code>2025-12-31 21:00</code>"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Hozir / Now", callback_data="gwstart_now")]])
    await query.edit_message_text(prompt, reply_markup=keyboard, parse_mode="HTML")
    return START_TIME


async def giveaway_start_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle start time: 'now' button or typed datetime → ask end time."""
    lang = context.user_data.get("lang", "en")

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        context.user_data["scheduled_start"] = None  # Immediate
    else:
        message = update.message
        text = message.text.strip()
        try:
            parsed = parse_user_datetime(text)
            if parsed <= datetime.utcnow():
                await message.reply_text("❌ Bu vaqt o'tgan. Kelajakdagi vaqt kiriting." if lang == "uz" else "❌ Time has passed.")
                return START_TIME
            context.user_data["scheduled_start"] = parsed
        except ValueError:
            await message.reply_text("❌ Format: <code>2025-12-31 21:00</code>", parse_mode="HTML")
            return START_TIME

    # Ask end time
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1h", callback_data="gwend_1h"), InlineKeyboardButton("6h", callback_data="gwend_6h"), InlineKeyboardButton("12h", callback_data="gwend_12h")],
        [InlineKeyboardButton("24h", callback_data="gwend_24h"), InlineKeyboardButton("3d", callback_data="gwend_3d"), InlineKeyboardButton("7d", callback_data="gwend_7d")],
        [InlineKeyboardButton("♾ Chegarasiz", callback_data="gwend_none")],
    ])
    prompt = "⌛️ <b>Tugash vaqti</b> (avtomatik qur'a):\n\nDavomiylik tanlang yoki sana kiriting: <code>2025-12-31 23:59</code>" if lang == "uz" else "⌛️ <b>End time</b> (auto-draw):\n\nChoose duration or enter date:"
    target = update.callback_query.message if update.callback_query else update.message
    if update.callback_query:
        await update.callback_query.edit_message_text(prompt, reply_markup=keyboard, parse_mode="HTML")
    else:
        await update.message.reply_text(prompt, reply_markup=keyboard, parse_mode="HTML")
    return END_TIME


async def giveaway_end_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle end time → ask sub channels."""
    lang = context.user_data.get("lang", "en")

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        dur_map = {"gwend_1h": timedelta(hours=1), "gwend_6h": timedelta(hours=6), "gwend_12h": timedelta(hours=12), "gwend_24h": timedelta(hours=24), "gwend_3d": timedelta(days=3), "gwend_7d": timedelta(days=7), "gwend_none": None}
        dur = dur_map.get(query.data)
        base = context.user_data.get("scheduled_start") or datetime.utcnow()
        context.user_data["ends_at"] = (base + dur) if dur else None
    else:
        message = update.message
        try:
            parsed = parse_user_datetime(message.text.strip())
            start = context.user_data.get("scheduled_start") or datetime.utcnow()
            if parsed <= start:
                await message.reply_text("❌ Tugash > boshlanish bo'lishi kerak." if lang == "uz" else "❌ End must be after start.")
                return END_TIME
            context.user_data["ends_at"] = parsed
        except ValueError:
            await message.reply_text("❌ Format: <code>2025-12-31 23:59</code>", parse_mode="HTML")
            return END_TIME

    # Ask subscription channels
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Kanal qo'shish" if lang == "uz" else "➕ Add channel", callback_data="gwsub_add")],
        [InlineKeyboardButton("⏭ O'tkazish" if lang == "uz" else "⏭ Skip", callback_data="gwsub_done")],
    ])
    prompt = "📢 <b>Majburiy obuna</b>\n\nIshtirokchilar obuna bo'lishi kerak kanallar (ixtiyoriy):" if lang == "uz" else "📢 <b>Required subscription</b> (optional):"
    target = update.callback_query.message if update.callback_query else update.message
    if update.callback_query:
        await update.callback_query.edit_message_text(prompt, reply_markup=keyboard, parse_mode="HTML")
    else:
        await update.message.reply_text(prompt, reply_markup=keyboard, parse_mode="HTML")
    return SUB_CHANNELS


async def giveaway_sub_channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle sub channel add/done."""
    lang = context.user_data.get("lang", "en")

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "gwsub_done":
            return await _ask_boost_channels(query, context)
        # Ask for channel input
        await query.edit_message_text("📢 Kanal @username yoki ID yuboring:" if lang == "uz" else "Send channel @username or ID:")
        return SUB_CHANNELS
    else:
        # Validate and add channel
        message = update.message
        ch = message.text.strip()
        channels_list = context.user_data.get("sub_channels_list", [])
        channels_list.append(ch)
        context.user_data["sub_channels_list"] = channels_list

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Yana qo'shish" if lang == "uz" else "➕ Add more", callback_data="gwsub_add")],
            [InlineKeyboardButton("✅ Tayyor" if lang == "uz" else "✅ Done", callback_data="gwsub_done")],
        ])
        await message.reply_text(f"✅ Qo'shildi: {ch}", reply_markup=keyboard)
        return SUB_CHANNELS


async def _ask_boost_channels(query, context) -> int:
    """Ask for optional boost channels (extra win-chance for subscribers)."""
    lang = context.user_data.get("lang", "en")
    prompts = {
        "uz": "🚀 <b>Bonus kanallar</b> (ixtiyoriy)\n\nBu kanallarga obuna bo'lgan ishtirokchilarning yutish ehtimoli oshadi.\nKanal @username larini vergul bilan yuboring yoki o'tkazib yuboring:",
        "ru": "🚀 <b>Бонусные каналы</b> (необязательно)\n\nПодписчики этих каналов получают повышенный шанс на победу.\nОтправьте @username через запятую или пропустите:",
        "en": "🚀 <b>Boost channels</b> (optional)\n\nSubscribers of these channels get a higher chance to win.\nSend @usernames comma-separated, or skip:",
    }
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ O'tkazish" if lang == "uz" else "⏭ Skip", callback_data="gwboost_skip")],
    ])
    await query.edit_message_text(prompts.get(lang, prompts["uz"]), reply_markup=keyboard, parse_mode="HTML")
    return BOOST_CHANNELS


async def giveaway_boost_channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle boost channels input (comma-separated) or skip → finalize."""
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        context.user_data["boost_channels"] = None
        return await _finalize_full_giveaway(query, context)

    channels = parse_channels(update.message.text)
    context.user_data["boost_channels"] = serialize_channels(channels)
    return await _finalize_full_giveaway(None, context, user=update.effective_user, chat_id=update.effective_chat.id, bot=context.bot)


async def _finalize_full_giveaway(query, context, user=None, chat_id=None, bot=None) -> int:
    """Save giveaway. QUEUED if scheduled, ACTIVE+publish if now.

    Called either with a callback `query` (user/chat derived from it) or, when
    the last step was a plain text message, with explicit user/chat_id/bot.
    """
    if query is not None:
        user = query.from_user
        chat_id = query.message.chat_id
        bot = context.bot
    lang = context.user_data.get("lang", "en")
    post_data = context.user_data["post_data"]
    scheduled_start = context.user_data.get("scheduled_start")
    sub_channels = context.user_data.get("sub_channels_list", [])
    channels_str = ",".join(sub_channels) if sub_channels else None
    button_label = context.user_data.get("button_label")
    status = "queued" if scheduled_start else "active"

    channels_pairs = context.user_data.get("channels_list") or []
    target_channels = ",".join(str(c[0]) for c in channels_pairs) if channels_pairs else None

    async with async_session() as session:
        giveaway = Giveaway(
            title=context.user_data["title"],
            post_text=post_data["post_text"],
            post_file_id=post_data["post_file_id"],
            post_media_type=post_data["post_media_type"],
            winner_count=context.user_data["winner_count"],
            required_channels=channels_str,
            button_label=button_label,
            boost_channels=context.user_data.get("boost_channels"),
            status=status,
            channel_id=context.user_data.get("channel_id"),
            target_channels=target_channels,
            scheduled_start=scheduled_start,
            creator_id=user.id,
            creator_username=user.username,
            chat_id=chat_id,
            ends_at=context.user_data.get("ends_at"),
        )
        session.add(giveaway)
        await session.commit()
        await session.refresh(giveaway)

    # If immediate, publish to every target channel now
    publish_error = None
    if not scheduled_start:
        if not _target_channel_ids(giveaway):
            giveaway.channel_id = chat_id  # last resort: creation chat
        posts, errors = await publish_giveaway_to_channels(bot, giveaway, lang)
        if errors:
            publish_error = "; ".join(f"{ch}: {str(e)[:60]}" for ch, e in errors)
        if not posts and not errors:
            publish_error = "kanal tanlanmagan"

    # Summary
    start_txt = fmt_local(scheduled_start, "Hozir")
    end_txt = fmt_local(context.user_data.get("ends_at"), "♾")
    ch_title = context.user_data.get("channel_title", "—")
    if publish_error:
        publish_line = f"❌ Kanalga joylab bo'lmadi: {publish_error[:100]}"
    elif scheduled_start:
        publish_line = "⏳ Avtomatik joylanadi."
    else:
        publish_line = "🟢 Joylandi!"
    summary = (
        f"✅ <b>O'yin yaratildi!</b>\n\n"
        f"📢 Kanal: <b>{ch_title}</b>\n"
        f"🏆 G'oliblar: {giveaway.winner_count}\n"
        f"⏳ Boshlanishi: {start_txt}\n"
        f"⌛️ Tugashi: {end_txt}\n"
        f"{publish_line}"
    )
    if query is not None:
        try:
            await query.delete_message()
        except Exception:
            pass
    await bot.send_message(chat_id, summary, parse_mode="HTML")

    # Share keyboard
    try:
        share_kb = _share_keyboard("gw", giveaway.id, lang)
        await bot.send_message(chat_id, "👆", reply_markup=share_kb)
    except Exception as e:
        logger.warning("Share keyboard failed (inline mode off?): %s", e)

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

        if giveaway.status != "active":
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
        btn_label = giveaway.button_label

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

    # NOTE: the channel post button counter is NOT updated here on purpose.
    # A dedicated job (refresh_giveaway_counters) refreshes every published
    # post at most once a minute, so a burst of joins can't flood Telegram's
    # message-edit rate limit. The user still sees their new count in the
    # answer toast above immediately.


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
                    Giveaway.status == "active",
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

        if giveaway.status != "active":
            await update.message.reply_text(get_text("gw_not_active", lang=lang))
            return

        participants = giveaway.participants
        if not participants:
            giveaway.status = "completed"
            giveaway.drawn_at = datetime.utcnow()
            await session.commit()
            await update.message.reply_text(get_text("gw_no_participants", lang=lang))
            # Also announce in channel
            try:
                ch = giveaway.channel_id or giveaway.chat_id
                await context.bot.send_message(ch, f"❌ <b>{giveaway.title}</b> — ishtirokchilar yo'q.", parse_mode="HTML")
            except Exception:
                pass
            return

        winners = await _weighted_draw(participants, giveaway.winner_count, giveaway.boost_channels, context.bot)

        for winner in winners:
            gw_winner = GiveawayWinner(
                giveaway_id=giveaway_id,
                user_id=winner.user_id,
                username=winner.username,
                first_name=winner.first_name,
            )
            session.add(gw_winner)

        giveaway.status = "completed"
        giveaway.drawn_at = datetime.utcnow()
        await session.commit()

    winners_text = "\n".join(
        f"🏆 {i+1}. {_format_user(w)}" for i, w in enumerate(winners)
    )

    # Announce in channel (reply to post), close the post, DM winners
    await announce_results(context.bot, giveaway, winners, len(participants))

    # Confirm to creator
    result_text = get_text(
        "gw_results", lang=lang,
        title=giveaway.title,
        prize=giveaway.prize or "",
        total=len(participants),
        winners=winners_text,
    )
    await update.message.reply_text(result_text, parse_mode="HTML")


# ─── Edit Menu ────────────────────────────────────────────────────────────────


def _edit_menu_keyboard(giveaway_id: int, lang: str) -> InlineKeyboardMarkup:
    """Build the edit menu inline keyboard (8 fields)."""
    labels = {
        "uz": ["⏳ Boshlanish vaqti", "⌛️ Tugash vaqti", "🏆 G'oliblar soni", "📑 Tavsif/Post", "🖼 Rasm/GIF", "✅ Obuna kanallar", "🎊 G'oliblar e'loni", "⏰ Eslatma matni"],
        "ru": ["⏳ Время начала", "⌛️ Время окончания", "🏆 Кол-во победителей", "📑 Описание", "🖼 Фото/GIF", "✅ Каналы подписки", "🎊 Текст итогов", "⏰ Текст напоминания"],
        "en": ["⏳ Start time", "⌛️ End time", "🏆 Winners", "📑 Description", "🖼 Photo/GIF", "✅ Sub channels", "🎊 Winner post", "⏰ Reminder text"],
    }
    btns = labels.get(lang, labels["uz"])
    gid = giveaway_id
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(btns[0], callback_data=f"gwedit_start_{gid}"),
         InlineKeyboardButton(btns[1], callback_data=f"gwedit_end_{gid}")],
        [InlineKeyboardButton(btns[2], callback_data=f"gwedit_winners_{gid}"),
         InlineKeyboardButton(btns[3], callback_data=f"gwedit_text_{gid}")],
        [InlineKeyboardButton(btns[4], callback_data=f"gwedit_photo_{gid}"),
         InlineKeyboardButton(btns[5], callback_data=f"gwedit_channels_{gid}")],
        [InlineKeyboardButton(btns[6], callback_data=f"gwedit_wintext_{gid}"),
         InlineKeyboardButton(btns[7], callback_data=f"gwedit_remind_{gid}")],
    ])


async def _show_edit_summary(bot, chat_id, giveaway, lang):
    """Send giveaway summary + edit menu."""
    start_txt = fmt_local(giveaway.scheduled_start, "Hozir/Joylangan")
    end_txt = fmt_local(giveaway.ends_at, "♾")
    ch_title = ""
    if giveaway.channel_id:
        try:
            chat_info = await bot.get_chat(giveaway.channel_id)
            ch_title = chat_info.title or str(giveaway.channel_id)
        except Exception:
            ch_title = str(giveaway.channel_id)

    status_labels = {"draft": "📝 Draft", "queued": "⏳ Navbatda", "active": "🟢 Faol", "completed": "✅ Tugagan", "cancelled": "❌ Bekor"}
    summary = (
        f"📋 <b>{giveaway.title}</b>\n\n"
        f"📊 Holat: {status_labels.get(giveaway.status, giveaway.status)}\n"
        f"📢 Kanal: {ch_title or '—'}\n"
        f"🏆 G'oliblar: {giveaway.winner_count}\n"
        f"⏳ Boshlanish: {start_txt}\n"
        f"⌛️ Tugash: {end_txt}\n"
        f"📢 Obuna: {giveaway.required_channels or '—'}\n"
    )
    kb = _edit_menu_keyboard(giveaway.id, lang)
    await bot.send_message(chat_id, summary, reply_markup=kb, parse_mode="HTML")


async def edit_giveaway_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show edit menu for a giveaway. Command: /edit <id>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    if not context.args:
        # Show list of editable giveaways
        async with async_session() as session:
            result = await session.execute(
                select(Giveaway).where(
                    Giveaway.creator_id == user_id,
                    Giveaway.status.in_(["draft", "queued", "active"]),
                )
            )
            giveaways = result.scalars().all()
        if not giveaways:
            await update.message.reply_text("Tahrir qilish uchun o'yinlar yo'q." if lang == "uz" else "No games to edit.")
            return
        gw_list = "\n".join(f"• <code>/edit {gw.id}</code> — {gw.title}" for gw in giveaways)
        await update.message.reply_text(f"✏️ <b>Tahrirlash</b>\n\n{gw_list}", parse_mode="HTML")
        return

    try:
        giveaway_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID noto'g'ri.")
        return

    async with async_session() as session:
        result = await session.execute(select(Giveaway).where(Giveaway.id == giveaway_id))
        giveaway = result.scalar_one_or_none()

    if not giveaway or giveaway.creator_id != user_id:
        await update.message.reply_text("❌ Topilmadi yoki sizniki emas.")
        return
    if giveaway.status == "completed" or giveaway.status == "cancelled":
        await update.message.reply_text("❌ Tugagan o'yinni tahrir qilib bo'lmaydi.")
        return

    await _show_edit_summary(context.bot, update.effective_chat.id, giveaway, lang)


async def edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle edit menu button press — ask for new value."""
    query = update.callback_query
    await query.answer()
    data = query.data  # gwedit_<field>_<id>
    parts = data.split("_")
    field = parts[1]
    giveaway_id = int(parts[2])
    lang = await get_user_lang(query.from_user.id)

    # Store editing state
    context.user_data["editing_gw_id"] = giveaway_id
    context.user_data["editing_field"] = field

    tpl_help = (
        "\n\nO'zgaruvchilar: <code>{winners}</code> — g'oliblar ro'yxati, <code>{title}</code> — sarlavha, "
        "<code>{count}</code> — ishtirokchilar soni, <code>{prize}</code> — sovg'a.\n<code>-</code> yuboring — standart matnga qaytarish"
        if lang == "uz" else
        "\n\nPlaceholders: <code>{winners}</code>, <code>{title}</code>, <code>{count}</code>, <code>{prize}</code>.\nSend <code>-</code> to reset to default"
    )
    prompts = {
        "start": "⏳ Yangi boshlanish vaqtini kiriting: <code>2025-12-31 21:00</code>" if lang == "uz" else "⏳ Enter new start time:",
        "end": "⌛️ Yangi tugash vaqtini kiriting: <code>2025-12-31 23:59</code>" if lang == "uz" else "⌛️ Enter new end time:",
        "winners": "🏆 Yangi g'oliblar sonini kiriting (raqam):" if lang == "uz" else "🏆 Enter new winner count:",
        "text": "📑 Yangi tavsif/post matnini yuboring:" if lang == "uz" else "📑 Send new description/post text:",
        "photo": "🖼 Yangi rasm yoki GIF yuboring (yoki matn yuboring o'chirish uchun):" if lang == "uz" else "🖼 Send new photo/GIF (or text to remove):",
        "channels": "📢 Yangi obuna kanallarni kiriting (vergul bilan):\nMisol: @kanal1, @kanal2\n\n<code>-</code> yuboring — o'chirish" if lang == "uz" else "📢 Enter channels (comma-separated) or send <code>-</code> to remove:",
        "wintext": ("🎊 G'oliblar e'lon qilinadigan post matnini yuboring. Qur'a tugagach bot shu matnni kanalga joylaydi." if lang == "uz" else "🎊 Send the winner-announcement post text.") + tpl_help,
        "remind": ("⏰ Tugashiga 1 soat qolganda kanalga chiqadigan eslatma matnini yuboring." if lang == "uz" else "⏰ Send the 1-hour reminder text.") + tpl_help,
    }
    await query.edit_message_text(prompts.get(field, "?"), parse_mode="HTML")


async def edit_field_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive the new value for the field being edited."""
    giveaway_id = context.user_data.get("editing_gw_id")
    field = context.user_data.get("editing_field")
    if not giveaway_id or not field:
        return  # Not in edit mode

    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    message = update.message

    async with async_session() as session:
        result = await session.execute(select(Giveaway).where(Giveaway.id == giveaway_id))
        giveaway = result.scalar_one_or_none()
        if not giveaway or giveaway.creator_id != user_id:
            context.user_data.pop("editing_gw_id", None)
            context.user_data.pop("editing_field", None)
            return

        updated = False
        if field == "start":
            try:
                parsed = parse_user_datetime(message.text)
                if parsed <= datetime.utcnow():
                    await message.reply_text("❌ Vaqt o'tgan." if lang == "uz" else "❌ Time has passed.")
                    return
                if giveaway.status == "active":
                    await message.reply_text("❌ O'yin allaqachon joylangan — boshlanish vaqtini o'zgartirib bo'lmaydi." if lang == "uz" else "❌ Already published — start time can't be changed.")
                    return
                giveaway.scheduled_start = parsed
                updated = True
            except ValueError:
                await message.reply_text("❌ Format: <code>2025-12-31 21:00</code>", parse_mode="HTML")
                return

        elif field == "end":
            try:
                parsed = parse_user_datetime(message.text)
                if parsed <= datetime.utcnow():
                    await message.reply_text("❌ Vaqt o'tgan." if lang == "uz" else "❌ Time has passed.")
                    return
                if giveaway.scheduled_start and parsed <= giveaway.scheduled_start:
                    await message.reply_text("❌ Tugash > boshlanish bo'lishi kerak." if lang == "uz" else "❌ End must be after start.")
                    return
                giveaway.ends_at = parsed
                updated = True
            except ValueError:
                await message.reply_text("❌ Format: <code>2025-12-31 23:59</code>", parse_mode="HTML")
                return

        elif field == "winners":
            try:
                count = int(message.text.strip())
                if count < 1:
                    raise ValueError
                giveaway.winner_count = count
                updated = True
            except ValueError:
                await message.reply_text("❌ Raqam kiriting (1+)." if lang == "uz" else "❌ Enter a number (1+).")
                return

        elif field == "text":
            post_data = _extract_post_data(message)
            giveaway.post_text = post_data["post_text"]
            if post_data["post_file_id"]:
                giveaway.post_file_id = post_data["post_file_id"]
                giveaway.post_media_type = post_data["post_media_type"]
            # Update title
            import re
            plain = re.sub(r"<[^>]+>", "", giveaway.post_text or "")
            giveaway.title = plain.strip().split("\n")[0][:100] or giveaway.title
            updated = True

        elif field == "photo":
            if message.photo:
                giveaway.post_file_id = message.photo[-1].file_id
                giveaway.post_media_type = "photo"
                updated = True
            elif message.document:
                giveaway.post_file_id = message.document.file_id
                giveaway.post_media_type = "document"
                updated = True
            elif message.animation:
                giveaway.post_file_id = message.animation.file_id
                giveaway.post_media_type = "animation"
                updated = True
            else:
                # Text message = remove photo
                giveaway.post_file_id = None
                giveaway.post_media_type = None
                updated = True

        elif field == "channels":
            text = message.text.strip()
            # "/skip" never reaches this handler (commands are filtered), so
            # also accept "-" / "skip" as the reset value.
            if text.lower() in ("/skip", "skip", "-"):
                giveaway.required_channels = None
            else:
                giveaway.required_channels = text
            updated = True

        elif field == "wintext":
            text = (message.text_html or message.text or "").strip()
            giveaway.winner_template = None if text.lower() in ("/skip", "skip", "-") else text
            updated = True

        elif field == "remind":
            text = (message.text_html or message.text or "").strip()
            giveaway.reminder_template = None if text.lower() in ("/skip", "skip", "-") else text
            updated = True

        if updated:
            await session.commit()
            await message.reply_text("✅ O'zgartirildi!" if lang == "uz" else "✅ Updated!")
            # Clear edit state
            context.user_data.pop("editing_gw_id", None)
            context.user_data.pop("editing_field", None)
            # Re-show edit menu
            await session.refresh(giveaway)
            await _show_edit_summary(context.bot, message.chat_id, giveaway, lang)


# ─── My Giveaways ───────────────────────────────────────────────────────────────


async def my_giveaways(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Paginated giveaway browser. Command: /mygiveaways"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    context.user_data["mygw_page"] = 0
    await _show_my_giveaway_page(context.bot, update.effective_chat.id, user_id, 0, lang)


async def _show_my_giveaway_page(bot, chat_id, user_id, page, lang, edit_message_id=None):
    """Show a single giveaway with Next/Back nav + actions."""
    async with async_session() as session:
        result = await session.execute(
            select(Giveaway)
            .options(selectinload(Giveaway.participants))
            .where(Giveaway.creator_id == user_id)
            .order_by(Giveaway.created_at.desc())
        )
        giveaways = result.scalars().all()

    if not giveaways:
        await bot.send_message(chat_id, get_text("gw_my_list_empty", lang=lang))
        return

    # Clamp page
    if page < 0:
        page = 0
    if page >= len(giveaways):
        page = len(giveaways) - 1

    gw = giveaways[page]
    total = len(giveaways)

    status_emoji = {
        "draft": "📝", "queued": "⏳",
        "active": "🟢", "completed": "✅", "cancelled": "❌",
    }
    emoji = status_emoji.get(gw.status, "❓")
    p_count = len(gw.participants)

    start_str = fmt_local(gw.scheduled_start)
    end_str = fmt_local(gw.ends_at)

    text = (
        f"{emoji} <b>{gw.title}</b>\n\n"
        f"📊 {gw.status} | 👤 {p_count} ishtirokchi\n"
        f"🏆 G'oliblar: {gw.winner_count}\n"
    )
    if start_str:
        text += f"⏳ Boshlanish: {start_str}\n"
    if end_str:
        text += f"⌛️ Tugash: {end_str}\n"
    text += f"\n📄 {page+1}/{total}"

    # Navigation + action buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"mygw_prev_{page}"))
    nav_buttons.append(InlineKeyboardButton(f"{page+1}/{total}", callback_data="mygw_noop"))
    if page < total - 1:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"mygw_next_{page}"))

    action_buttons = []
    if gw.status == "active":
        action_buttons.append(InlineKeyboardButton("🎲 Qur'a", callback_data=f"mygw_draw_{gw.id}"))
        action_buttons.append(InlineKeyboardButton("❌ Bekor", callback_data=f"mygw_cancel_{gw.id}"))
    if gw.status in ("draft", "queued", "active"):
        action_buttons.append(InlineKeyboardButton("✏️ Tahrir", callback_data=f"mygw_edit_{gw.id}"))

    rows = [nav_buttons]
    if action_buttons:
        rows.append(action_buttons)

    kb = InlineKeyboardMarkup(rows)

    if edit_message_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=edit_message_id, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")


async def my_giveaways_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Next/Back/Draw/Cancel/Edit in paginated browser."""
    query = update.callback_query
    await query.answer()
    data = query.data  # mygw_<action>_<value>
    parts = data.split("_")
    action = parts[1]

    user_id = query.from_user.id
    lang = await get_user_lang(user_id)

    if action == "prev":
        page = int(parts[2]) - 1
        context.user_data["mygw_page"] = max(page, 0)
        await _show_my_giveaway_page(context.bot, query.message.chat_id, user_id, page, lang, edit_message_id=query.message.message_id)

    elif action == "next":
        page = int(parts[2]) + 1
        context.user_data["mygw_page"] = page
        await _show_my_giveaway_page(context.bot, query.message.chat_id, user_id, page, lang, edit_message_id=query.message.message_id)

    elif action == "noop":
        pass

    elif action == "draw":
        giveaway_id = int(parts[2])
        # Quick-draw from browser
        async with async_session() as session:
            result = await session.execute(
                select(Giveaway).options(selectinload(Giveaway.participants)).where(Giveaway.id == giveaway_id)
            )
            giveaway = result.scalar_one_or_none()
            if not giveaway or giveaway.creator_id != user_id or giveaway.status != "active":
                await query.answer("❌", show_alert=True)
                return
            if not giveaway.participants:
                await query.answer("Ishtirokchilar yo'q", show_alert=True)
                return
            total = len(giveaway.participants)
            winners = await _weighted_draw(giveaway.participants, giveaway.winner_count, giveaway.boost_channels, context.bot)
            for w in winners:
                session.add(GiveawayWinner(giveaway_id=giveaway_id, user_id=w.user_id, username=w.username, first_name=w.first_name))
            giveaway.status = "completed"
            giveaway.drawn_at = datetime.utcnow()
            await session.commit()

        # Announce in channel (reply to post), close the post, DM winners
        await announce_results(context.bot, giveaway, winners, total)

        await query.answer("🎉 Qur'a o'tkazildi!")
        page = context.user_data.get("mygw_page", 0)
        await _show_my_giveaway_page(context.bot, query.message.chat_id, user_id, page, lang, edit_message_id=query.message.message_id)

    elif action == "cancel":
        giveaway_id = int(parts[2])
        async with async_session() as session:
            result = await session.execute(select(Giveaway).where(Giveaway.id == giveaway_id))
            gw = result.scalar_one_or_none()
            if gw and gw.creator_id == user_id and gw.status in ("active", "queued", "draft"):
                gw.status = "cancelled"
                await session.commit()
        await query.answer("❌ Bekor qilindi")
        page = context.user_data.get("mygw_page", 0)
        await _show_my_giveaway_page(context.bot, query.message.chat_id, user_id, page, lang, edit_message_id=query.message.message_id)

    elif action == "edit":
        giveaway_id = int(parts[2])
        async with async_session() as session:
            result = await session.execute(select(Giveaway).where(Giveaway.id == giveaway_id))
            gw = result.scalar_one_or_none()
        if gw and gw.creator_id == user_id:
            await _show_edit_summary(context.bot, query.message.chat_id, gw, lang)


# ─── My Channels ──────────────────────────────────────────────────────────────


async def my_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's saved channels + add new channel button. Command: /mychannels"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    bot_username = (await context.bot.get_me()).username

    # Fetch saved channels
    async with async_session() as session:
        from sqlalchemy import and_
        result = await session.execute(
            select(UserChannel).where(UserChannel.user_id == user_id)
            .order_by(UserChannel.added_at.desc())
        )
        channels = result.scalars().all()

    headers = {
        "uz": "📢 <b>Mening kanallarim</b>\n\nBot admin bo'lgan kanallar ro'yxati:",
        "ru": "📢 <b>Мои каналы</b>\n\nСписок каналов, где бот является админом:",
        "en": "📢 <b>My Channels</b>\n\nChannels where bot is admin:",
    }
    text = headers.get(lang, headers["uz"])

    if channels:
        for i, ch in enumerate(channels, 1):
            name = ch.chat_title or str(ch.chat_id)
            username = f" (@{ch.chat_username})" if ch.chat_username else ""
            text += f"\n{i}. <b>{name}</b>{username}\n   ID: <code>{ch.chat_id}</code>"
    else:
        empty = {
            "uz": "\n\n📭 Hali kanal qo'shilmagan.\nPastdagi tugma orqali botni kanalingizga admin qiling.",
            "ru": "\n\n📭 Каналов пока нет.\nДобавьте бота как админа через кнопку ниже.",
            "en": "\n\n📭 No channels yet.\nAdd the bot as admin using the button below.",
        }
        text += empty.get(lang, empty["uz"])

    # Buttons: Add to channel + Add to group
    add_labels = {
        "uz": ("➕ Kanalga qo'shish", "➕ Guruhga qo'shish", "🔄 Yangilash"),
        "ru": ("➕ Добавить в канал", "➕ В группу", "🔄 Обновить"),
        "en": ("➕ Add to channel", "➕ Add to group", "🔄 Refresh"),
    }
    ch_label, gr_label, ref_label = add_labels.get(lang, add_labels["uz"])

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(ch_label, url=f"https://t.me/{bot_username}?startchannel&admin=post_messages+edit_messages+delete_messages+invite_users")],
        [InlineKeyboardButton(gr_label, url=f"https://t.me/{bot_username}?startgroup=true&admin=post_messages+invite_users+manage_chat")],
        [InlineKeyboardButton(ref_label, callback_data="mych_refresh")],
    ])

    text += "\n\n💡 " + (
        "Bot kanalga qo'shilgandan keyin, o'sha kanaldan biror xabarni shu yerga forward qiling — avtomatik qo'shiladi."
        if lang == "uz" else
        "After adding bot, forward any message from that channel here — it will be saved automatically."
    )

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def my_channels_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh channels list — check which saved channels the bot is still admin of."""
    query = update.callback_query
    await query.answer("🔄")
    user_id = query.from_user.id
    lang = await get_user_lang(user_id)

    # Verify each saved channel (remove ones where bot is no longer admin)
    async with async_session() as session:
        result = await session.execute(
            select(UserChannel).where(UserChannel.user_id == user_id)
        )
        channels = result.scalars().all()

        removed = 0
        for ch in channels:
            try:
                member = await context.bot.get_chat_member(ch.chat_id, context.bot.id)
                if member.status not in ("administrator", "creator"):
                    await session.delete(ch)
                    removed += 1
                else:
                    # Update title
                    try:
                        chat_info = await context.bot.get_chat(ch.chat_id)
                        ch.chat_title = chat_info.title
                        ch.chat_username = chat_info.username
                    except Exception:
                        pass
            except Exception:
                await session.delete(ch)
                removed += 1
        await session.commit()

    msg = f"🔄 Yangilandi! {f'{removed} ta olib tashlandi.' if removed else 'Hammasi joyida.'}" if lang == "uz" else f"🔄 Refreshed! {f'{removed} removed.' if removed else 'All good.'}"
    await query.answer(msg, show_alert=True)


async def my_channels_forward_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """When user forwards a message from a channel, auto-save that channel."""
    message = update.message
    if not message:
        return

    # Check forward_origin for channel
    channel_id = None
    channel_title = None

    if hasattr(message, 'forward_origin') and message.forward_origin:
        fo = message.forward_origin
        if hasattr(fo, 'chat') and fo.chat:
            channel_id = fo.chat.id
            channel_title = fo.chat.title

    if not channel_id:
        return  # Not a forwarded channel message

    user_id = message.from_user.id

    # Verify bot is admin there
    try:
        member = await context.bot.get_chat_member(channel_id, context.bot.id)
        if member.status not in ("administrator", "creator"):
            lang = await get_user_lang(user_id)
            await message.reply_text(
                "❌ Bot bu kanalda admin emas. Avval botni admin qiling." if lang == "uz"
                else "❌ Bot is not admin in this channel. Add bot as admin first."
            )
            return
    except Exception:
        return

    # Save channel
    async with async_session() as session:
        from sqlalchemy import and_
        existing = await session.execute(
            select(UserChannel).where(and_(UserChannel.user_id == user_id, UserChannel.chat_id == channel_id))
        )
        if not existing.scalar_one_or_none():
            try:
                chat_info = await context.bot.get_chat(channel_id)
                session.add(UserChannel(
                    user_id=user_id, chat_id=channel_id,
                    chat_title=chat_info.title, chat_username=chat_info.username,
                ))
            except Exception:
                session.add(UserChannel(user_id=user_id, chat_id=channel_id, chat_title=channel_title))
            await session.commit()

    lang = await get_user_lang(user_id)
    await message.reply_text(
        f"✅ Kanal qo'shildi: <b>{channel_title}</b>\n\nEndi /newgiveaway orqali o'yin yaratishda shu kanal avtomatik taklif qilinadi."
        if lang == "uz" else f"✅ Channel added: <b>{channel_title}</b>",
        parse_mode="HTML",
    )


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

        if giveaway.status != "active":
            await update.message.reply_text(get_text("gw_cancel_not_active", lang=lang))
            return

        giveaway.status = "cancelled"
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


# ─── Notify Participants ─────────────────────────────────────────────────────


NOTIFY_MSG = 100  # state for notify conversation


async def notify_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start notifying participants. Command: /notify <giveaway_id>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    if not context.args:
        # Show list of active giveaways to pick from
        async with async_session() as session:
            result = await session.execute(
                select(Giveaway).where(
                    Giveaway.creator_id == user_id,
                    Giveaway.status == "active",
                )
            )
            giveaways = result.scalars().all()

        if not giveaways:
            msg = {"uz": "Sizda faol o'yinlar yo'q.", "ru": "У вас нет активных игр.", "en": "You have no active games."}
            await update.message.reply_text(msg.get(lang, msg["uz"]))
            return ConversationHandler.END

        gw_list = "\n".join(f"• <code>/notify {gw.id}</code> — {gw.title}" for gw in giveaways)
        msg = {"uz": f"📢 Qaysi o'yinga xabar yubormoqchisiz?\n\n{gw_list}",
               "ru": f"📢 Для какой игры отправить уведомление?\n\n{gw_list}",
               "en": f"📢 Which game to notify?\n\n{gw_list}"}
        await update.message.reply_text(msg.get(lang, msg["uz"]), parse_mode="HTML")
        return ConversationHandler.END

    try:
        giveaway_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID")
        return ConversationHandler.END

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).where(Giveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

    if not giveaway:
        await update.message.reply_text("❌ Not found")
        return ConversationHandler.END
    if giveaway.creator_id != user_id:
        msg = {"uz": "❌ Faqat yaratuvchi xabar yubora oladi.", "ru": "❌ Только создатель может уведомлять.", "en": "❌ Only the creator can notify."}
        await update.message.reply_text(msg.get(lang, msg["uz"]))
        return ConversationHandler.END

    context.user_data["notify_giveaway_id"] = giveaway_id
    context.user_data["notify_lang"] = lang

    msg = {"uz": f"📢 <b>{giveaway.title}</b> ishtirokchilariga xabar yuboring.\n\nXabar matnini hozir yozing (matn, rasm, video — istalgan format):",
           "ru": f"📢 Отправьте сообщение участникам <b>{giveaway.title}</b>.\n\nНапишите текст уведомления (текст, фото, видео — любой формат):",
           "en": f"📢 Send a message to <b>{giveaway.title}</b> participants.\n\nType the notification now (text, photo, video — any format):"}
    await update.message.reply_text(msg.get(lang, msg["uz"]), parse_mode="HTML")
    return NOTIFY_MSG


async def notify_receive_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the notification message and send to all participants."""
    lang = context.user_data.get("notify_lang", "en")
    giveaway_id = context.user_data.get("notify_giveaway_id")

    if not giveaway_id:
        return ConversationHandler.END

    message = update.message
    post_data = _extract_post_data(message)

    # Get all participants
    async with async_session() as session:
        result = await session.execute(
            select(GiveawayParticipant).where(
                GiveawayParticipant.giveaway_id == giveaway_id
            )
        )
        participants = result.scalars().all()

    if not participants:
        msg = {"uz": "❌ Bu o'yinda ishtirokchilar yo'q.", "ru": "❌ В этой игре нет участников.", "en": "❌ No participants in this game."}
        await message.reply_text(msg.get(lang, msg["uz"]))
        context.user_data.pop("notify_giveaway_id", None)
        context.user_data.pop("notify_lang", None)
        return ConversationHandler.END

    # Send to each participant
    sent = 0
    failed = 0
    for p in participants:
        try:
            if post_data["post_file_id"] and post_data["post_media_type"]:
                mt = post_data["post_media_type"]
                caption = post_data["post_text"] or ""
                if mt == "photo":
                    await context.bot.send_photo(p.user_id, post_data["post_file_id"], caption=caption, parse_mode="HTML")
                elif mt == "video":
                    await context.bot.send_video(p.user_id, post_data["post_file_id"], caption=caption, parse_mode="HTML")
                else:
                    await context.bot.send_message(p.user_id, caption or "📢", parse_mode="HTML")
            else:
                await context.bot.send_message(p.user_id, post_data["post_text"] or "📢", parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1

    msg = {"uz": f"✅ Xabar yuborildi!\n\n📨 Yuborildi: {sent}\n❌ Muvaffaqiyatsiz: {failed}",
           "ru": f"✅ Уведомление отправлено!\n\n📨 Отправлено: {sent}\n❌ Не удалось: {failed}",
           "en": f"✅ Notification sent!\n\n📨 Sent: {sent}\n❌ Failed: {failed}"}
    await message.reply_text(msg.get(lang, msg["uz"]), parse_mode="HTML")

    context.user_data.pop("notify_giveaway_id", None)
    context.user_data.pop("notify_lang", None)
    return ConversationHandler.END


async def notify_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel notify."""
    context.user_data.pop("notify_giveaway_id", None)
    context.user_data.pop("notify_lang", None)
    await update.message.reply_text("❌ Bekor qilindi.")
    return ConversationHandler.END


# ─── Handler Registration ────────────────────────────────────────────────────────


def get_giveaway_handlers() -> list:
    """Return all giveaway-related handlers."""
    create_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newgiveaway", new_giveaway_start),
            # Reply-menu button must be an entry point too — calling the entry
            # function from a plain MessageHandler never starts the conversation.
            MessageHandler(
                filters.Regex(r"^(🎲 O'yin yaratish|🎲 Создать игру|🎲 Create Game)$"),
                new_giveaway_start,
            ),
        ],
        states={
            POST: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL)
                    & ~filters.COMMAND,
                    giveaway_receive_post,
                ),
            ],
            PREVIEW: [CallbackQueryHandler(giveaway_preview_response, pattern=r"^gwprev_")],
            BUTTON_LABEL: [
                CallbackQueryHandler(giveaway_button_label_handler, pattern=r"^gwbtn_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, giveaway_button_label_handler),
            ],
            CHANNEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, giveaway_channel_entered),
                CallbackQueryHandler(giveaway_channel_more_callback, pattern=r"^gwch_"),
            ],
            WINNERS: [CallbackQueryHandler(giveaway_winners_selected, pattern=r"^gwwin_")],
            START_TIME: [
                CallbackQueryHandler(giveaway_start_time_handler, pattern=r"^gwstart_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, giveaway_start_time_handler),
            ],
            END_TIME: [
                CallbackQueryHandler(giveaway_end_time_handler, pattern=r"^gwend_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, giveaway_end_time_handler),
            ],
            SUB_CHANNELS: [
                CallbackQueryHandler(giveaway_sub_channels_handler, pattern=r"^gwsub_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, giveaway_sub_channels_handler),
            ],
            BOOST_CHANNELS: [
                CallbackQueryHandler(giveaway_boost_channels_handler, pattern=r"^gwboost_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, giveaway_boost_channels_handler),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_creation)],
    )

    notify_conv = ConversationHandler(
        entry_points=[CommandHandler("notify", notify_start)],
        states={
            NOTIFY_MSG: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL)
                    & ~filters.COMMAND,
                    notify_receive_message,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", notify_cancel)],
    )

    return [
        create_conv,
        notify_conv,
        CommandHandler("draw", draw_giveaway),
        CommandHandler("edit", edit_giveaway_command),
        CommandHandler("mygiveaways", my_giveaways),
        CommandHandler("mychannels", my_channels_command),
        CommandHandler("cancelgiveaway", cancel_giveaway),
        CallbackQueryHandler(join_giveaway_callback, pattern=r"^join_gw_\d+$"),
        CallbackQueryHandler(edit_field_callback, pattern=r"^gwedit_"),
        CallbackQueryHandler(my_giveaways_nav_callback, pattern=r"^mygw_"),
        CallbackQueryHandler(my_channels_refresh_callback, pattern=r"^mych_refresh$"),
    ]
