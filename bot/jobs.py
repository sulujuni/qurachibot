"""Scheduled jobs: auto-draw, reminders, deadline enforcement."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telegram.ext import ContextTypes

from bot.models.database import async_session
from bot.models.giveaway import Giveaway, GiveawayStatus, GiveawayParticipant, GiveawayWinner
from bot.models.contest import Contest, ContestStatus
from bot.models.group_giveaway import (
    GroupGiveaway,
    GroupGiveawayMode,
    GroupGiveawayStatus,
    GroupGiveawayWinner,
)
from bot.models.notification import AlertSubscription, ScheduledReminder
from bot.i18n.core import get_text
from bot.utils.lang import get_user_lang

logger = logging.getLogger(__name__)


def _format_winner(w, index: int) -> str:
    """Format a winner row for announcement text."""
    name = f"@{w.username}" if w.username else (w.first_name or f"User {w.user_id}")
    return f"🏆 {index}. {name}"


def _mention_winner(w, index: int) -> str:
    """Format winner with tg://user mention link (works for users without @username)."""
    if w.username:
        return f'🏆 {index}. <a href="https://t.me/{w.username}">@{w.username}</a>'
    name = w.first_name or f"User {w.user_id}"
    return f'🏆 {index}. <a href="tg://user?id={w.user_id}">{name}</a>'


# ─── Scheduled Publish Timer ─────────────────────────────────────────────────


async def publish_queued_giveaways(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Publish QUEUED giveaways whose scheduled_start time has arrived.

    Runs every 10 seconds. Moves giveaways from QUEUED → ACTIVE,
    sends the post to the channel, and notifies the creator.
    """
    now = datetime.utcnow()

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).where(
                Giveaway.status == GiveawayStatus.QUEUED,
                Giveaway.scheduled_start <= now,
                Giveaway.scheduled_start != None,
            )
        )
        queued = result.scalars().all()

    for gw in queued:
        channel_id = gw.channel_id or gw.chat_id
        lang = await get_user_lang(gw.creator_id)

        # Build join button
        from bot.config import settings
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
        join_label = f"🎮 {get_text('gw_join_button', lang=lang)} (0)"
        web_url = settings.WEB_URL
        if web_url:
            url = f"{web_url.rstrip('/')}/miniapp/giveaway?id={gw.id}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(join_label, web_app=WebAppInfo(url=url))]])
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(join_label, callback_data=f"join_gw_{gw.id}")]])

        # Send the post to the channel
        try:
            if gw.post_file_id and gw.post_media_type:
                mt = gw.post_media_type
                caption = gw.post_text or ""
                if mt == "photo":
                    msg = await context.bot.send_photo(channel_id, gw.post_file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
                elif mt == "video":
                    msg = await context.bot.send_video(channel_id, gw.post_file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
                elif mt == "animation":
                    msg = await context.bot.send_animation(channel_id, gw.post_file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
                else:
                    msg = await context.bot.send_document(channel_id, gw.post_file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
            else:
                msg = await context.bot.send_message(channel_id, gw.post_text or gw.title, parse_mode="HTML", reply_markup=kb)

            # Update status to ACTIVE
            async with async_session() as session:
                result = await session.execute(select(Giveaway).where(Giveaway.id == gw.id))
                g = result.scalar_one()
                g.status = GiveawayStatus.ACTIVE
                g.published_at = now
                g.message_id = msg.message_id
                if not g.channel_id:
                    g.channel_id = channel_id
                await session.commit()

            # Notify creator
            try:
                published_msg = {
                    "uz": f"✅ <b>{gw.title}</b> kanalga muvaffaqiyatli joylandi!",
                    "ru": f"✅ <b>{gw.title}</b> опубликован в канале!",
                    "en": f"✅ <b>{gw.title}</b> published to channel!",
                }
                await context.bot.send_message(gw.creator_id, published_msg.get(lang, published_msg["uz"]), parse_mode="HTML")
            except Exception:
                pass

            logger.info(f"Published queued giveaway {gw.id} to channel {channel_id}")

        except Exception as e:
            logger.error(f"Failed to publish queued giveaway {gw.id}: {e}")
            # Notify creator about failure
            try:
                fail_msg = {
                    "uz": f"❌ <b>{gw.title}</b> kanalga joylab bo'lmadi: {str(e)[:100]}",
                    "ru": f"❌ Не удалось опубликовать <b>{gw.title}</b>: {str(e)[:100]}",
                    "en": f"❌ Failed to publish <b>{gw.title}</b>: {str(e)[:100]}",
                }
                await context.bot.send_message(gw.creator_id, fail_msg.get(lang, fail_msg["uz"]), parse_mode="HTML")
            except Exception:
                pass
            # Mark as cancelled so it doesn't retry forever
            async with async_session() as session:
                result = await session.execute(select(Giveaway).where(Giveaway.id == gw.id))
                g = result.scalar_one()
                g.status = GiveawayStatus.CANCELLED
                await session.commit()


async def check_expired_giveaways(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check for giveaways that have expired and auto-draw them."""
    import random

    now = datetime.utcnow()

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway)
            .options(selectinload(Giveaway.participants))
            .where(
                Giveaway.status == GiveawayStatus.ACTIVE,
                Giveaway.ends_at <= now,
                Giveaway.ends_at != None,
            )
        )
        expired_giveaways = result.scalars().all()

        for giveaway in expired_giveaways:
            if not giveaway.participants:
                giveaway.status = GiveawayStatus.COMPLETED
                giveaway.drawn_at = now
                await session.commit()
                # Announce "no winners" in channel
                announce_channel = giveaway.channel_id or giveaway.chat_id
                try:
                    await context.bot.send_message(
                        announce_channel,
                        f"❌ <b>{giveaway.title}</b>\n\n"
                        f"Ishtirokchilar bo'lmaganligi sababli g'olib aniqlanmadi.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                # Notify creator
                try:
                    await context.bot.send_message(
                        giveaway.creator_id,
                        f"❌ <b>{giveaway.title}</b> — ishtirokchilar bo'lmaganligi sababli tugadi.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                continue

            # Draw winners
            winner_count = min(giveaway.winner_count, len(giveaway.participants))
            winners = random.sample(list(giveaway.participants), winner_count)

            for winner in winners:
                gw_winner = GiveawayWinner(
                    giveaway_id=giveaway.id,
                    user_id=winner.user_id,
                    username=winner.username,
                    first_name=winner.first_name,
                )
                session.add(gw_winner)

            giveaway.status = GiveawayStatus.COMPLETED
            giveaway.drawn_at = now
            await session.commit()

            # Announce winners publicly
            winners_text = "\n".join(
                _mention_winner(w, i+1) for i, w in enumerate(winners)
            )
            # Announce in the channel where post was published
            announce_channel = giveaway.channel_id or giveaway.chat_id
            try:
                await context.bot.send_message(
                    announce_channel,
                    f"🎊 <b>{giveaway.title}</b>\n\n"
                    f"👤 {get_text('gw_participants', lang='uz')}: {len(giveaway.participants)}\n\n"
                    f"<b>🏆 G'oliblar:</b>\n{winners_text}\n\n"
                    f"Tabriklaymiz! 🎉",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Failed to announce giveaway {giveaway.id} in channel: {e}")

            # Also notify the creator via DM
            try:
                lang = await get_user_lang(giveaway.creator_id)
                creator_msg = {
                    "uz": f"✅ <b>{giveaway.title}</b> tugadi!\n\nG'oliblar:\n{winners_text}",
                    "ru": f"✅ <b>{giveaway.title}</b> завершён!\n\nПобедители:\n{winners_text}",
                    "en": f"✅ <b>{giveaway.title}</b> ended!\n\nWinners:\n{winners_text}",
                }
                await context.bot.send_message(
                    giveaway.creator_id,
                    creator_msg.get(lang, creator_msg["uz"]),
                    parse_mode="HTML",
                )
            except Exception:
                pass

            # DM each winner
            for winner in winners:
                try:
                    w_lang = await get_user_lang(winner.user_id)
                    win_msg = {
                        "uz": f"🎉 <b>Tabriklaymiz!</b>\n\n<b>{giveaway.title}</b> yutuqli o'yinida g'olib bo'ldingiz!\n🎁 {giveaway.prize or ''}\n\nTashkilotchi bilan bog'laning!",
                        "ru": f"🎉 <b>Поздравляем!</b>\n\nВы победили в розыгрыше <b>{giveaway.title}</b>!\n🎁 {giveaway.prize or ''}\n\nСвяжитесь с организатором!",
                        "en": f"🎉 <b>Congratulations!</b>\n\nYou won: <b>{giveaway.title}</b>!\n🎁 {giveaway.prize or ''}\n\nContact the organizer to claim!",
                    }
                    await context.bot.send_message(
                        winner.user_id, win_msg.get(w_lang, win_msg["uz"]), parse_mode="HTML",
                    )
                except Exception:
                    pass  # User may have blocked the bot

    logger.info(f"Checked expired giveaways. Processed: {len(expired_giveaways)}")


async def check_expired_group_giveaways(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-draw time-limited group/channel comment giveaways that have expired."""
    import random

    from bot.handlers.group_giveaway import _active_giveaway_posts, _channel_post_giveaways
    from bot.i18n import get_text
    from bot.utils.lang import get_user_lang
    from bot.utils.loyalty import award_points

    now = datetime.utcnow()

    async with async_session() as session:
        result = await session.execute(
            select(GroupGiveaway)
            .options(selectinload(GroupGiveaway.entries))
            .where(
                GroupGiveaway.status == GroupGiveawayStatus.ACTIVE,
                GroupGiveaway.ends_at <= now,
                GroupGiveaway.ends_at != None,
            )
        )
        expired = result.scalars().all()

        for giveaway in expired:
            valid_entries = [e for e in giveaway.entries if e.is_valid]

            # Clean up in-memory tracking maps regardless of outcome
            _active_giveaway_posts.pop((giveaway.chat_id, giveaway.message_id), None)
            _channel_post_giveaways.pop((giveaway.chat_id, giveaway.message_id), None)

            creator_lang = await get_user_lang(giveaway.creator_id)

            if not valid_entries:
                giveaway.status = GroupGiveawayStatus.CANCELLED
                await session.commit()
                try:
                    await context.bot.send_message(
                        giveaway.chat_id,
                        get_text("gg_no_participants_expired", lang=creator_lang, title=giveaway.title),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                continue

            # FIRST_N picks earliest entries; other modes pick randomly
            winner_count = min(giveaway.winner_count, len(valid_entries))
            if giveaway.mode == GroupGiveawayMode.FIRST_N:
                winners = sorted(valid_entries, key=lambda e: e.entered_at)[:winner_count]
            else:
                winners = random.sample(valid_entries, winner_count)

            for w in winners:
                session.add(GroupGiveawayWinner(
                    giveaway_id=giveaway.id,
                    user_id=w.user_id, username=w.username, first_name=w.first_name,
                ))

            giveaway.status = GroupGiveawayStatus.COMPLETED
            giveaway.drawn_at = now
            await session.commit()

            # Award loyalty points to winners
            for w in winners:
                try:
                    await award_points(w.user_id, "win_giveaway", username=w.username)
                except Exception:
                    pass

            winners_text = "\n".join(_format_winner(w, i + 1) for i, w in enumerate(winners))
            try:
                await context.bot.send_message(
                    giveaway.chat_id,
                    get_text(
                        "gg_results", lang=creator_lang,
                        title=giveaway.title, prize=giveaway.prize,
                        total=len(valid_entries), winners=winners_text,
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Failed to announce group giveaway {giveaway.id}: {e}")

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

    logger.info(f"Checked expired group giveaways. Processed: {len(expired)}")


async def check_submission_deadlines(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check contests whose submission deadline has passed."""
    now = datetime.utcnow()

    async with async_session() as session:
        result = await session.execute(
            select(Contest).where(
                Contest.status == ContestStatus.ACCEPTING_SUBMISSIONS,
                Contest.submissions_end_at <= now,
                Contest.submissions_end_at != None,
            )
        )
        expired_contests = result.scalars().all()

        for contest in expired_contests:
            contest.status = ContestStatus.VOTING
            await session.commit()

            try:
                await context.bot.send_message(
                    contest.chat_id,
                    f"🗳 <b>Submissions closed: {contest.title}</b>\n\n"
                    f"The submission deadline has passed. Voting is now open!\n"
                    f"Use: <code>/vote &lt;submission_id&gt;</code>",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Failed to announce contest deadline {contest.id}: {e}")

    logger.info(f"Checked submission deadlines. Processed: {len(expired_contests)}")


async def send_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send ending-soon reminders (1 hour before deadline)."""
    now = datetime.utcnow()
    reminder_window = now + timedelta(hours=1)

    async with async_session() as session:
        # Giveaways ending within 1 hour
        result = await session.execute(
            select(Giveaway)
            .options(selectinload(Giveaway.participants))
            .where(
                Giveaway.status == GiveawayStatus.ACTIVE,
                Giveaway.ends_at <= reminder_window,
                Giveaway.ends_at > now,
            )
        )
        ending_giveaways = result.scalars().all()

        for gw in ending_giveaways:
            # Check if reminder already sent
            result = await session.execute(
                select(ScheduledReminder).where(
                    ScheduledReminder.event_type == "giveaway",
                    ScheduledReminder.event_id == gw.id,
                    ScheduledReminder.sent == True,
                )
            )
            if result.scalar_one_or_none():
                continue

            try:
                await context.bot.send_message(
                    gw.chat_id,
                    f"⏰ <b>ENDING SOON: {gw.title}</b>\n\n"
                    f"This giveaway ends in less than 1 hour!\n"
                    f"👤 Current participants: {len(gw.participants)}\n"
                    f"🎁 Prize: {gw.prize}\n\n"
                    f"Last chance to join! 🏃",
                    parse_mode="HTML",
                )
                # Mark as sent
                reminder = ScheduledReminder(
                    event_type="giveaway",
                    event_id=gw.id,
                    chat_id=gw.chat_id,
                    remind_at=now,
                    sent=True,
                )
                session.add(reminder)
                await session.commit()
            except Exception as e:
                logger.error(f"Failed to send reminder for giveaway {gw.id}: {e}")


async def send_new_event_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send alerts to subscribers about new events created in last 5 minutes."""
    now = datetime.utcnow()
    since = now - timedelta(minutes=5)

    async with async_session() as session:
        # New giveaways
        result = await session.execute(
            select(Giveaway).where(
                Giveaway.created_at >= since,
                Giveaway.status == GiveawayStatus.ACTIVE,
                Giveaway.is_test == False,
            )
        )
        new_giveaways = result.scalars().all()

        # New contests
        result = await session.execute(
            select(Contest).where(
                Contest.created_at >= since,
                Contest.status == ContestStatus.ACCEPTING_SUBMISSIONS,
            )
        )
        new_contests = result.scalars().all()

        if not new_giveaways and not new_contests:
            return

        # Get subscribers
        result = await session.execute(select(AlertSubscription))
        subscribers = result.scalars().all()

    for sub in subscribers:
        try:
            lang = await get_user_lang(sub.user_id)
        except Exception:
            lang = "en"
        try:
            for gw in new_giveaways:
                if gw.creator_id == sub.user_id:
                    continue  # Don't notify creator
                lines = [
                    f"🔔 <b>{get_text('alert_giveaway_header', lang)}</b>",
                    "",
                    f"🎉 <b>{gw.title}</b>",
                ]
                if gw.prize:
                    lines.append(f"🎁 {get_text('alert_prize_label', lang)}: {gw.prize}")
                lines.append(f"🏆 {get_text('alert_winners_label', lang)}: {gw.winner_count}")
                lines.append("")
                lines.append(get_text("alert_giveaway_cta", lang))

                # Build Join button
                from bot.config import settings
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
                web_url = settings.WEB_URL
                join_label = f"🎮 {get_text('gw_join_button', lang=lang)}"
                if web_url:
                    url = f"{web_url.rstrip('/')}/miniapp/giveaway?id={gw.id}"
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton(join_label, web_app=WebAppInfo(url=url))]])
                else:
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton(join_label, callback_data=f"join_gw_{gw.id}")]])

                await context.bot.send_message(
                    sub.user_id,
                    "\n".join(lines),
                    parse_mode="HTML",
                    reply_markup=kb,
                )

            for ct in new_contests:
                if ct.creator_id == sub.user_id:
                    continue
                lines = [
                    f"🔔 <b>{get_text('alert_contest_header', lang)}</b>",
                    "",
                    f"🏅 <b>{ct.title}</b>",
                ]
                if ct.prize:
                    lines.append(f"🎁 {get_text('alert_prize_label', lang)}: {ct.prize}")
                lines.append("")
                lines.append(get_text("alert_contest_cta", lang))

                # Build Submit button
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"📤 {get_text('ct_submit_button', lang=lang)}",
                        callback_data=f"submit_contest_{ct.id}"
                    )
                ]])

                await context.bot.send_message(
                    sub.user_id,
                    "\n".join(lines),
                    parse_mode="HTML",
                    reply_markup=kb,
                )
        except Exception:
            pass  # User blocked bot or other error
