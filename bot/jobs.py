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
                giveaway.status = GiveawayStatus.CANCELLED
                await session.commit()
                try:
                    await context.bot.send_message(
                        giveaway.chat_id,
                        f"❌ Giveaway <b>'{giveaway.title}'</b> expired with no participants.",
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

            # Announce
            winners_text = "\n".join(
                f"🏆 {i+1}. @{w.username}" if w.username else f"🏆 {i+1}. {w.first_name or f'User {w.user_id}'}"
                for i, w in enumerate(winners)
            )
            try:
                await context.bot.send_message(
                    giveaway.chat_id,
                    f"🎊 <b>AUTO-DRAW: {giveaway.title}</b>\n\n"
                    f"🎁 Prize: {giveaway.prize}\n"
                    f"👤 Participants: {len(giveaway.participants)}\n\n"
                    f"<b>Winners:</b>\n{winners_text}\n\n"
                    f"Congratulations! 🎉",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Failed to announce giveaway {giveaway.id}: {e}")

            # DM winners
            for winner in winners:
                try:
                    await context.bot.send_message(
                        winner.user_id,
                        f"🎉 <b>Congratulations!</b>\n\n"
                        f"You won the giveaway: <b>{giveaway.title}</b>\n"
                        f"🎁 Prize: {giveaway.prize}\n\n"
                        f"Contact the organizer to claim your prize!",
                        parse_mode="HTML",
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
                await context.bot.send_message(
                    sub.user_id,
                    "\n".join(lines),
                    parse_mode="HTML",
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
                await context.bot.send_message(
                    sub.user_id,
                    "\n".join(lines),
                    parse_mode="HTML",
                )
        except Exception:
            pass  # User blocked bot or other error
