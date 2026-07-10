"""Admin panel with dynamic stats, blacklist management, and content moderation."""

from sqlalchemy import select, func

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.i18n import get_text
from bot.models import (
    Giveaway, GiveawayStatus, GiveawayParticipant, GiveawayWinner,
    Contest, ContestStatus, ContestSubmission, ContestVote,
    async_session,
)
from bot.models.loyalty import LoyaltyPoints
from bot.models.moderation import Blacklist, ContentFlag
from bot.models.referral import Referral
from bot.utils.lang import get_user_lang
from bot.utils.moderation import add_to_blacklist, remove_from_blacklist

# Bot admin IDs (set via environment or config)
import os
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]


def is_admin(user_id: int) -> bool:
    """Check if user is a bot admin."""
    return user_id in ADMIN_IDS


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show admin panel. Command: /admin"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Access denied. Admins only.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Live Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🎉 Giveaway Stats", callback_data="admin_gw_stats"),
         InlineKeyboardButton("🏅 Contest Stats", callback_data="admin_ct_stats")],
        [InlineKeyboardButton("👥 User Stats", callback_data="admin_user_stats")],
        [InlineKeyboardButton("🚫 Blacklist", callback_data="admin_blacklist"),
         InlineKeyboardButton("🚩 Flagged Content", callback_data="admin_flags")],
        [InlineKeyboardButton("📈 Referral Stats", callback_data="admin_referrals")],
    ])

    await update.message.reply_text(
        "🛡 <b>Admin Panel</b>\n\nSelect a section:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show live stats overview."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Access denied", show_alert=True)
        return
    await query.answer()

    async with async_session() as session:
        # Giveaway stats
        gw_total = (await session.execute(select(func.count(Giveaway.id)))).scalar()
        gw_active = (await session.execute(
            select(func.count(Giveaway.id)).where(Giveaway.status == "active")
        )).scalar()
        gw_participants = (await session.execute(select(func.count(GiveawayParticipant.id)))).scalar()
        gw_winners = (await session.execute(select(func.count(GiveawayWinner.id)))).scalar()

        # Contest stats
        ct_total = (await session.execute(select(func.count(Contest.id)))).scalar()
        ct_active = (await session.execute(
            select(func.count(Contest.id)).where(Contest.status == ContestStatus.ACCEPTING_SUBMISSIONS)
        )).scalar()
        ct_submissions = (await session.execute(select(func.count(ContestSubmission.id)))).scalar()
        ct_votes = (await session.execute(select(func.count(ContestVote.id)))).scalar()

        # User stats
        unique_participants = (await session.execute(
            select(func.count(func.distinct(GiveawayParticipant.user_id)))
        )).scalar()

        # Referrals
        total_referrals = (await session.execute(select(func.count(Referral.id)))).scalar()

        # Moderation
        blacklisted = (await session.execute(
            select(func.count(Blacklist.id)).where(Blacklist.is_active == True)
        )).scalar()
        flagged = (await session.execute(
            select(func.count(ContentFlag.id)).where(ContentFlag.resolved == False)
        )).scalar()

    text = (
        "📊 <b>LIVE STATS</b>\n\n"
        f"<b>🎉 Giveaways:</b>\n"
        f"  Total: {gw_total} | Active: {gw_active}\n"
        f"  Participants: {gw_participants} | Winners: {gw_winners}\n\n"
        f"<b>🏅 Contests:</b>\n"
        f"  Total: {ct_total} | Active: {ct_active}\n"
        f"  Submissions: {ct_submissions} | Votes: {ct_votes}\n\n"
        f"<b>👥 Users:</b>\n"
        f"  Unique participants: {unique_participants}\n"
        f"  Referrals: {total_referrals}\n\n"
        f"<b>🛡 Moderation:</b>\n"
        f"  Blacklisted: {blacklisted}\n"
        f"  Flagged (unresolved): {flagged}"
    )

    back_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_stats")],
        [InlineKeyboardButton("◀️ Back", callback_data="admin_back")],
    ])

    await query.edit_message_text(text, reply_markup=back_keyboard, parse_mode="HTML")


async def admin_gw_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show detailed giveaway stats."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).order_by(Giveaway.created_at.desc()).limit(10)
        )
        giveaways = result.scalars().all()

    status_map = {"active": "🟢", "completed": "✅", "cancelled": "❌"}
    text = "🎉 <b>Recent Giveaways:</b>\n\n"
    for gw in giveaways:
        emoji = status_map.get(gw.status.value, "❓")
        text += f"{emoji} <b>{gw.title}</b> (ID:{gw.id})\n   by @{gw.creator_username or gw.creator_id}\n\n"

    if not giveaways:
        text += "No giveaways yet."

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]])
    await query.edit_message_text(text, reply_markup=back_kb, parse_mode="HTML")


async def admin_ct_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show detailed contest stats."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()

    async with async_session() as session:
        result = await session.execute(
            select(Contest).order_by(Contest.created_at.desc()).limit(10)
        )
        contests = result.scalars().all()

    text = "🏅 <b>Recent Contests:</b>\n\n"
    for ct in contests:
        text += f"📋 <b>{ct.title}</b> (ID:{ct.id}) — {ct.status.value}\n"

    if not contests:
        text += "No contests yet."

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]])
    await query.edit_message_text(text, reply_markup=back_kb, parse_mode="HTML")


async def admin_user_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user/loyalty stats."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()

    async with async_session() as session:
        result = await session.execute(
            select(LoyaltyPoints).order_by(LoyaltyPoints.total_earned.desc()).limit(10)
        )
        top_users = result.scalars().all()

    text = "👥 <b>Top Users by Points:</b>\n\n"
    for i, u in enumerate(top_users, 1):
        name = f"@{u.username}" if u.username else (u.first_name or f"User {u.user_id}")
        text += f"{i}. {name} — {u.total_earned} pts (🏆 {u.wins} wins)\n"

    if not top_users:
        text += "No users yet."

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]])
    await query.edit_message_text(text, reply_markup=back_kb, parse_mode="HTML")


async def admin_blacklist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show blacklisted users."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()

    async with async_session() as session:
        result = await session.execute(
            select(Blacklist).where(Blacklist.is_active == True).limit(20)
        )
        banned = result.scalars().all()

    text = "🚫 <b>Blacklisted Users:</b>\n\n"
    for b in banned:
        name = f"@{b.username}" if b.username else f"ID:{b.user_id}"
        text += f"• {name} — {b.reason or 'No reason'}\n"

    if not banned:
        text += "No blacklisted users.\n"

    text += "\nUse /ban <user_id> [reason] to ban.\nUse /unban <user_id> to unban."

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]])
    await query.edit_message_text(text, reply_markup=back_kb, parse_mode="HTML")


async def admin_flags_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show flagged content."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()

    async with async_session() as session:
        result = await session.execute(
            select(ContentFlag).where(ContentFlag.resolved == False).limit(10)
        )
        flags = result.scalars().all()

    text = "🚩 <b>Flagged Content (Unresolved):</b>\n\n"
    for f in flags:
        preview = (f.content_text[:60] + "...") if f.content_text and len(f.content_text) > 60 else (f.content_text or "N/A")
        text += f"#{f.id} | User {f.user_id} | {f.reason}\n  \"{preview}\"\n\n"

    if not flags:
        text += "No unresolved flags! 🎉"

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]])
    await query.edit_message_text(text, reply_markup=back_kb, parse_mode="HTML")


async def admin_referrals_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show referral stats."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()

    async with async_session() as session:
        total = (await session.execute(select(func.count(Referral.id)))).scalar()
        # Top referrers
        result = await session.execute(
            select(Referral.referrer_id, Referral.referrer_username, func.count(Referral.id).label("cnt"))
            .group_by(Referral.referrer_id, Referral.referrer_username)
            .order_by(func.count(Referral.id).desc())
            .limit(10)
        )
        top = result.all()

    text = f"📈 <b>Referral Stats</b>\n\nTotal referrals: {total}\n\n<b>Top Referrers:</b>\n"
    for i, (uid, uname, cnt) in enumerate(top, 1):
        name = f"@{uname}" if uname else f"User {uid}"
        text += f"{i}. {name} — {cnt} referrals\n"

    if not top:
        text += "No referrals yet."

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]])
    await query.edit_message_text(text, reply_markup=back_kb, parse_mode="HTML")


async def admin_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to main admin panel."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Live Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🎉 Giveaway Stats", callback_data="admin_gw_stats"),
         InlineKeyboardButton("🏅 Contest Stats", callback_data="admin_ct_stats")],
        [InlineKeyboardButton("👥 User Stats", callback_data="admin_user_stats")],
        [InlineKeyboardButton("🚫 Blacklist", callback_data="admin_blacklist"),
         InlineKeyboardButton("🚩 Flagged Content", callback_data="admin_flags")],
        [InlineKeyboardButton("📈 Referral Stats", callback_data="admin_referrals")],
    ])

    await query.edit_message_text(
        "🛡 <b>Admin Panel</b>\n\nSelect a section:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


# ─── Ban/Unban Commands ──────────────────────────────────────────────────────────


# ─── Broadcast Command ────────────────────────────────────────────────────────


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Broadcast a message to all users. Command: /broadcast <message>

    Usage:
      /broadcast Salom! Yangi o'yin boshlandi!
      /broadcast <b>Muhim xabar!</b> Kanalimizga obuna bo'ling.
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Faqat adminlar uchun.")
        return

    if not context.args:
        await update.message.reply_text(
            "📢 <b>Xabar tarqatish</b>\n\n"
            "Foydalanish:\n"
            "<code>/broadcast Sizning xabaringiz...</code>\n\n"
            "HTML formatlash qo'llab-quvvatlanadi:\n"
            "<code>&lt;b&gt;qalin&lt;/b&gt;</code>, <code>&lt;i&gt;kursiv&lt;/i&gt;</code>, "
            "<code>&lt;a href=\"url\"&gt;havola&lt;/a&gt;</code>",
            parse_mode="HTML",
        )
        return

    broadcast_text = update.message.text.split(None, 1)[1]  # everything after /broadcast

    # Get all user IDs
    from bot.models.user_settings import UserSettings
    result_msg = await update.message.reply_text("📢 Yuborilmoqda...")

    async with async_session() as session:
        result = await session.execute(select(UserSettings.user_id))
        user_ids = [r[0] for r in result.all()]
        # Also participants
        result = await session.execute(
            select(func.distinct(GiveawayParticipant.user_id))
        )
        user_ids_set = set(user_ids)
        user_ids_set.update(r[0] for r in result.all())

    sent = 0
    failed = 0
    for uid in user_ids_set:
        try:
            await context.bot.send_message(uid, broadcast_text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1

    await result_msg.edit_text(
        f"📢 <b>Xabar tarqatish tugadi!</b>\n\n"
        f"✅ Yuborildi: {sent}\n"
        f"❌ Xatolik: {failed}\n"
        f"📊 Jami: {len(user_ids_set)}",
        parse_mode="HTML",
    )


# ─── Ban/Unban Commands ──────────────────────────────────────────────────────────


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ban a user. Command: /ban <user_id> [reason]"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id> [reason]")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    reason = " ".join(context.args[1:]) if len(context.args) > 1 else None
    await add_to_blacklist(target_id, update.effective_user.id, reason=reason)
    await update.message.reply_text(f"🚫 User {target_id} has been banned.\nReason: {reason or 'None'}")


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unban a user. Command: /unban <user_id>"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    removed = await remove_from_blacklist(target_id)
    if removed:
        await update.message.reply_text(f"✅ User {target_id} has been unbanned.")
    else:
        await update.message.reply_text(f"ℹ️ User {target_id} was not blacklisted.")


# ─── Handler Registration ────────────────────────────────────────────────────────


def get_admin_handlers() -> list:
    """Return admin panel handlers."""
    return [
        CommandHandler("admin", admin_panel),
        CommandHandler("ban", ban_user),
        CommandHandler("unban", unban_user),
        CommandHandler("broadcast", broadcast_command),
        CallbackQueryHandler(admin_stats_callback, pattern=r"^admin_stats$"),
        CallbackQueryHandler(admin_gw_stats_callback, pattern=r"^admin_gw_stats$"),
        CallbackQueryHandler(admin_ct_stats_callback, pattern=r"^admin_ct_stats$"),
        CallbackQueryHandler(admin_user_stats_callback, pattern=r"^admin_user_stats$"),
        CallbackQueryHandler(admin_blacklist_callback, pattern=r"^admin_blacklist$"),
        CallbackQueryHandler(admin_flags_callback, pattern=r"^admin_flags$"),
        CallbackQueryHandler(admin_referrals_callback, pattern=r"^admin_referrals$"),
        CallbackQueryHandler(admin_back_callback, pattern=r"^admin_back$"),
    ]
