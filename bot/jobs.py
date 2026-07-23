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
                Giveaway.status == "queued",
                Giveaway.scheduled_start <= now,
                Giveaway.scheduled_start != None,
            )
        )
        queued = result.scalars().all()

    for gw in queued:
        lang = await get_user_lang(gw.creator_id)

        # Already past its end time (e.g. bot was down) — cancel instead of
        # publishing a giveaway that would immediately expire.
        if gw.ends_at and gw.ends_at <= now:
            async with async_session() as session:
                g = (await session.execute(select(Giveaway).where(Giveaway.id == gw.id))).scalar_one()
                g.status = "cancelled"
                await session.commit()
            try:
                await context.bot.send_message(
                    gw.creator_id,
                    f"❌ <b>{gw.title}</b> — joylash vaqti o'tkazib yuborildi (tugash vaqti ham o'tgan), bekor qilindi.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            continue

        # Publish to every target channel (multi-channel support)
        from telegram.error import BadRequest, Forbidden, InvalidToken
        from bot.handlers.giveaway import publish_giveaway_to_channels

        posts, errors = await publish_giveaway_to_channels(context.bot, gw, lang)

        if posts:
            async with async_session() as session:
                result = await session.execute(select(Giveaway).where(Giveaway.id == gw.id))
                g = result.scalar_one()
                g.status = "active"
                await session.commit()

            # Notify creator (mention partial failures, if any)
            try:
                published_msg = {
                    "uz": f"✅ <b>{gw.title}</b> {len(posts)} ta kanalga joylandi!",
                    "ru": f"✅ <b>{gw.title}</b> опубликован в {len(posts)} канал(ах)!",
                    "en": f"✅ <b>{gw.title}</b> published to {len(posts)} channel(s)!",
                }
                text = published_msg.get(lang, published_msg["uz"])
                if errors:
                    text += "\n⚠️ " + "; ".join(f"{ch}: {str(e)[:60]}" for ch, e in errors)
                await context.bot.send_message(gw.creator_id, text, parse_mode="HTML")
            except Exception:
                pass
            logger.info(f"Published queued giveaway {gw.id} to {len(posts)} channel(s)")
            continue

        # Nothing published. Transient network problems → leave QUEUED to retry.
        if errors and not any(isinstance(e, (BadRequest, Forbidden, InvalidToken)) for _, e in errors):
            continue

        # Permanent error (kicked from channel, bad content, …) — notify + cancel
        err_txt = "; ".join(f"{ch}: {str(e)[:60]}" for ch, e in errors) if errors else "kanal topilmadi"
        try:
            fail_msg = {
                "uz": f"❌ <b>{gw.title}</b> kanalga joylab bo'lmadi: {err_txt}",
                "ru": f"❌ Не удалось опубликовать <b>{gw.title}</b>: {err_txt}",
                "en": f"❌ Failed to publish <b>{gw.title}</b>: {err_txt}",
            }
            await context.bot.send_message(gw.creator_id, fail_msg.get(lang, fail_msg["uz"]), parse_mode="HTML")
        except Exception:
            pass
        async with async_session() as session:
            result = await session.execute(select(Giveaway).where(Giveaway.id == gw.id))
            g = result.scalar_one()
            g.status = "cancelled"
            await session.commit()


async def refresh_giveaway_counters(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh join-button counters on every active giveaway post (once/min).

    Counters are intentionally NOT updated on each join — a popular giveaway
    could edit its channel posts dozens of times a second and hit Telegram's
    message-edit rate limit. Instead this job batches the update: it runs once
    a minute and (thanks to the dirty-check inside update_all_post_counters)
    only re-edits posts whose participant count actually changed.
    """
    from bot.handlers.giveaway import update_all_post_counters

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway.id).where(Giveaway.status == "active")
        )
        active_ids = result.scalars().all()

    for gid in active_ids:
        try:
            await update_all_post_counters(context.bot, gid)
        except Exception as e:
            logger.debug("Counter refresh failed for giveaway %s: %s", gid, e)


async def check_expired_giveaways(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check for giveaways that have expired and auto-draw them."""
    import random

    now = datetime.utcnow()

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway)
            .options(selectinload(Giveaway.participants))
            .where(
                Giveaway.status == "active",
                Giveaway.ends_at <= now,
                Giveaway.ends_at != None,
            )
        )
        expired_giveaways = result.scalars().all()

        for giveaway in expired_giveaways:
            if not giveaway.participants:
                giveaway.status = "completed"
                giveaway.drawn_at = now
                await session.commit()
                # Announce "no winners" in every channel + remove join buttons
                from bot.handlers.giveaway import close_published_post, get_published_posts
                no_winner_text = (
                    f"❌ <b>{giveaway.title}</b>\n\n"
                    f"Ishtirokchilar bo'lmaganligi sababli g'olib aniqlanmadi."
                )
                posts = await get_published_posts(giveaway)
                if posts:
                    for p_chat, p_msg in posts:
                        try:
                            await context.bot.send_message(
                                p_chat, no_winner_text, parse_mode="HTML",
                                reply_to_message_id=p_msg,
                            )
                        except Exception:
                            pass
                else:
                    try:
                        await context.bot.send_message(giveaway.chat_id, no_winner_text, parse_mode="HTML")
                    except Exception:
                        pass
                await close_published_post(context.bot, giveaway)
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

            # Draw winners (weighted by boost-channel subscriptions if set)
            from bot.handlers.giveaway import _weighted_draw
            winners = await _weighted_draw(
                giveaway.participants, giveaway.winner_count, giveaway.boost_channels, context.bot,
            )

            for winner in winners:
                gw_winner = GiveawayWinner(
                    giveaway_id=giveaway.id,
                    user_id=winner.user_id,
                    username=winner.username,
                    first_name=winner.first_name,
                )
                session.add(gw_winner)

            giveaway.status = "completed"
            giveaway.drawn_at = now
            await session.commit()

            # Announce in channel (reply to post), close the post, DM winners
            from bot.handlers.giveaway import announce_results
            winners_text = "\n".join(
                _mention_winner(w, i+1) for i, w in enumerate(winners)
            )
            await announce_results(context.bot, giveaway, winners, len(giveaway.participants))

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
                Giveaway.status == "active",
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

            # Remind in every channel where the post lives (reply to each post),
            # falling back to the creation chat for unpublished giveaways.
            # Owner's reminder_template wins over the built-in text.
            from bot.handlers.giveaway import get_published_posts, render_owner_template
            creator_lang = await get_user_lang(gw.creator_id)
            if gw.reminder_template and gw.reminder_template.strip():
                reminder_text = render_owner_template(
                    gw.reminder_template,
                    title=gw.title, count=len(gw.participants), prize=gw.prize,
                )
            else:
                reminder_msgs = {
                    "uz": f"⏰ <b>Tez orada tugaydi: {gw.title}</b>\n\nBu o'yin 1 soatdan kamroq vaqtda tugaydi!\n👤 Ishtirokchilar: {len(gw.participants)}\n\nOxirgi imkoniyat! 🏃",
                    "ru": f"⏰ <b>Скоро завершится: {gw.title}</b>\n\nРозыгрыш закончится менее чем через 1 час!\n👤 Участников: {len(gw.participants)}\n\nПоследний шанс! 🏃",
                    "en": f"⏰ <b>ENDING SOON: {gw.title}</b>\n\nThis giveaway ends in less than 1 hour!\n👤 Participants: {len(gw.participants)}\n\nLast chance to join! 🏃",
                }
                reminder_text = reminder_msgs.get(creator_lang, reminder_msgs["uz"])

            posts = await get_published_posts(gw)
            targets = posts if posts else [(gw.chat_id, None)]
            sent_any = False
            for t_chat, t_msg in targets:
                kwargs = {"reply_to_message_id": t_msg} if t_msg else {}
                try:
                    await context.bot.send_message(
                        t_chat, reminder_text, parse_mode="HTML", **kwargs,
                    )
                    sent_any = True
                except Exception as e:
                    logger.error(f"Failed to send reminder for giveaway {gw.id} to {t_chat}: {e}")

            if sent_any:
                # Mark as sent
                reminder = ScheduledReminder(
                    event_type="giveaway",
                    event_id=gw.id,
                    chat_id=targets[0][0],
                    remind_at=now,
                    sent=True,
                )
                session.add(reminder)
                await session.commit()


async def send_new_event_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send alerts to subscribers about new events created in last 5 minutes."""
    now = datetime.utcnow()
    since = now - timedelta(minutes=5)

    async with async_session() as session:
        # New giveaways
        result = await session.execute(
            select(Giveaway).where(
                Giveaway.created_at >= since,
                Giveaway.status == "active",
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
