"""Scheduled jobs: auto-draw, reminders, deadline enforcement."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telegram.ext import ContextTypes

from bot.models.database import async_session
from bot.models.giveaway import Giveaway, GiveawayStatus, GiveawayParticipant, GiveawayWinner
from bot.models.contest import Contest, ContestStatus
from bot.models.notification import AlertSubscription, ScheduledReminder

logger = logging.getLogger(__name__)


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
            for gw in new_giveaways:
                if gw.creator_id == sub.user_id:
                    continue  # Don't notify creator
                await context.bot.send_message(
                    sub.user_id,
                    f"🔔 <b>New Giveaway!</b>\n\n"
                    f"🎉 {gw.title}\n"
                    f"🎁 Prize: {gw.prize}\n"
                    f"🏆 Winners: {gw.winner_count}\n\n"
                    f"Join before it ends!",
                    parse_mode="HTML",
                )

            for ct in new_contests:
                if ct.creator_id == sub.user_id:
                    continue
                await context.bot.send_message(
                    sub.user_id,
                    f"🔔 <b>New Contest!</b>\n\n"
                    f"🏅 {ct.title}\n"
                    f"📂 Type: {ct.contest_type.value}\n"
                    f"{f'🎁 Prize: {ct.prize}' if ct.prize else ''}\n\n"
                    f"Submit your entry now!",
                    parse_mode="HTML",
                )
        except Exception:
            pass  # User blocked bot or other error
