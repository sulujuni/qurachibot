"""Admin panel with dynamic stats, blacklist management, and content moderation."""

import html
import json
import os
import random
from datetime import datetime

from sqlalchemy import select, func, text as sql_text

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
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Same on-disk files the Mini App admin panel reads/writes (web/miniapp_api.py) —
# start_production.sh's auto-updater/trigger watcher poll these, regardless of
# which process (bot or web) wrote them.
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPDATE_CONFIG_PATH = os.path.join(_BASE_DIR, "update_config.json")
RESTART_TRIGGER_PATH = os.path.join(_BASE_DIR, ".restart_trigger")


def is_admin(user_id: int) -> bool:
    """Check if user is a bot admin."""
    return user_id in ADMIN_IDS


def _admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Live Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🎉 Giveaway Stats", callback_data="admin_gw_stats"),
         InlineKeyboardButton("🏅 Contest Stats", callback_data="admin_ct_stats")],
        [InlineKeyboardButton("👥 User Stats", callback_data="admin_user_stats"),
         InlineKeyboardButton("🔢 User Counter", callback_data="admin_users_count")],
        [InlineKeyboardButton("🚫 Blacklist", callback_data="admin_blacklist"),
         InlineKeyboardButton("🚩 Flagged Content", callback_data="admin_flags")],
        [InlineKeyboardButton("📈 Referral Stats", callback_data="admin_referrals"),
         InlineKeyboardButton("📝 Feedback", callback_data="admin_feedback")],
        [InlineKeyboardButton("🎁 Manage Giveaways", callback_data="admin_gw_manage")],
        [InlineKeyboardButton("💾 DB Info", callback_data="admin_dbinfo"),
         InlineKeyboardButton("🔄 Restart Control", callback_data="admin_restart")],
    ])


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show admin panel. Command: /admin"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Access denied. Admins only.")
        return

    await update.message.reply_text(
        "🛡 <b>Admin Panel</b>\n\nSelect a section:",
        reply_markup=_admin_main_keyboard(),
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

    await query.edit_message_text(
        "🛡 <b>Admin Panel</b>\n\nSelect a section:",
        reply_markup=_admin_main_keyboard(),
        parse_mode="HTML",
    )


# ─── User Counter (ported from Mini App admin tab) ───────────────────────────


async def admin_users_count_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show total user counts from different sources."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()

    from bot.models.user_settings import UserSettings

    async with async_session() as session:
        started = (await session.execute(select(func.count(UserSettings.id)))).scalar() or 0
        participants = (await session.execute(
            select(func.count(func.distinct(GiveawayParticipant.user_id)))
        )).scalar() or 0
        with_points = (await session.execute(select(func.count(LoyaltyPoints.id)))).scalar() or 0
        verified = (await session.execute(
            select(func.count(UserSettings.id)).where(UserSettings.captcha_verified == True)
        )).scalar() or 0
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_active = (await session.execute(
            select(func.count(func.distinct(GiveawayParticipant.user_id)))
            .where(GiveawayParticipant.joined_at >= today)
        )).scalar() or 0

    text = (
        "🔢 <b>User Counter</b>\n\n"
        f"Started bot: {started}\n"
        f"Joined a giveaway: {participants}\n"
        f"Have loyalty points: {with_points}\n"
        f"Verified (passed CAPTCHA): {verified}\n"
        f"Active today: {today_active}"
    )

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]])
    await query.edit_message_text(text, reply_markup=back_kb, parse_mode="HTML")


# ─── DB Info (ported from Mini App admin tab) ────────────────────────────────


async def admin_dbinfo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show database size and row counts."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()

    key_tables = [
        "user_settings", "giveaways", "giveaway_participants",
        "contests", "group_giveaways", "referrals", "loyalty_points",
    ]

    async with async_session() as session:
        db_size = "?"
        try:
            result = await session.execute(sql_text("SELECT pg_size_pretty(pg_database_size(current_database()))"))
            db_size = result.scalar()
        except Exception:
            pass

        row_counts = {}
        for table_name in key_tables:
            try:
                result = await session.execute(sql_text(f"SELECT count(*) FROM {table_name}"))
                row_counts[table_name] = result.scalar() or 0
            except Exception:
                row_counts[table_name] = -1

    text = f"💾 <b>DB Info</b>\n\nSize: {db_size}\n\n"
    text += "\n".join(f"<code>{t}</code>: {c}" for t, c in row_counts.items())

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]])
    await query.edit_message_text(text, reply_markup=back_kb, parse_mode="HTML")


# ─── Feedback viewer (ported from Mini App admin tab) ────────────────────────


async def admin_feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent user feedback/suggestions/complaints."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()

    async with async_session() as session:
        result = await session.execute(
            select(ContentFlag)
            .where(ContentFlag.content_type.like("feedback_%"))
            .order_by(ContentFlag.flagged_at.desc())
            .limit(15)
        )
        items = result.scalars().all()

    text = "📝 <b>Recent Feedback</b>\n\n"
    if items:
        for f in items:
            raw_preview = (f.content_text[:100] + "...") if f.content_text and len(f.content_text) > 100 else (f.content_text or "N/A")
            reason = html.escape(f.reason or "")
            preview = html.escape(raw_preview)
            text += f"<b>{reason}</b> (User {f.user_id})\n\"{preview}\"\n\n"
    else:
        text += "No feedback yet."

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]])
    await query.edit_message_text(text, reply_markup=back_kb, parse_mode="HTML")


# ─── Giveaway management: force-draw / cancel (ported from Mini App) ─────────


async def _render_gw_manage(query) -> None:
    """Render the giveaway-management list onto an already-answered callback query."""
    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).where(Giveaway.status == "active")
            .order_by(Giveaway.created_at.desc()).limit(10)
        )
        giveaways = result.scalars().all()

    rows = []
    lines = ["🎁 <b>Manage Giveaways</b>\n"]
    if not giveaways:
        lines.append("No active giveaways.")
    for gw in giveaways:
        lines.append(f"• <b>{html.escape(gw.title)}</b> (ID:{gw.id})")
        rows.append([
            InlineKeyboardButton(f"🎲 Draw #{gw.id}", callback_data=f"admin_draw_{gw.id}"),
            InlineKeyboardButton(f"❌ Cancel #{gw.id}", callback_data=f"admin_cancel_{gw.id}"),
        ])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data="admin_back")])

    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML"
    )


async def admin_gw_manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List active giveaways with Draw/Cancel controls."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()
    await _render_gw_manage(query)


async def admin_draw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-draw winners for a giveaway (admin override, no scheduled wait)."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return

    giveaway_id = int(query.data.split("_")[-1])

    async with async_session() as session:
        result = await session.execute(select(Giveaway).where(Giveaway.id == giveaway_id))
        giveaway = result.scalar_one_or_none()
        if not giveaway:
            await query.answer("Not found", show_alert=True)
            return
        if giveaway.status != "active":
            await query.answer("This giveaway is not active", show_alert=True)
            return

        result = await session.execute(
            select(GiveawayParticipant).where(GiveawayParticipant.giveaway_id == giveaway_id)
        )
        participants = result.scalars().all()
        if not participants:
            await query.answer("No participants", show_alert=True)
            return

        winner_count = min(giveaway.winner_count, len(participants))
        winners = random.sample(list(participants), winner_count)

        for w in winners:
            session.add(GiveawayWinner(
                giveaway_id=giveaway_id,
                user_id=w.user_id, username=w.username, first_name=w.first_name,
            ))

        giveaway.status = "completed"
        giveaway.drawn_at = datetime.utcnow()
        await session.commit()

    winner_names = [f"@{w.username}" if w.username else (w.first_name or f"ID:{w.user_id}") for w in winners]
    alert_text = f"✅ Winners: {', '.join(winner_names)}"
    if len(alert_text) > 200:
        alert_text = alert_text[:197] + "..."
    await query.answer(alert_text, show_alert=True)
    await _render_gw_manage(query)


async def admin_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel an active giveaway."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return

    giveaway_id = int(query.data.split("_")[-1])

    async with async_session() as session:
        result = await session.execute(select(Giveaway).where(Giveaway.id == giveaway_id))
        giveaway = result.scalar_one_or_none()
        if not giveaway:
            await query.answer("Not found", show_alert=True)
            return
        if giveaway.status != "active":
            await query.answer("This giveaway is not active", show_alert=True)
            return

        giveaway.status = "cancelled"
        await session.commit()

    await query.answer("🚫 Giveaway cancelled", show_alert=True)
    await _render_gw_manage(query)


# ─── Restart control (ported from Mini App admin tab) ────────────────────────


def _read_restart_config() -> dict:
    try:
        with open(UPDATE_CONFIG_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"restart_hour": 3, "restart_minute": 0, "jitter_minutes": 30, "enabled": True}


def _write_restart_config(config: dict) -> None:
    """Write update_config.json atomically (temp file + rename) so a concurrent
    reader in the web process (which also writes this file) never sees a
    torn/partial write."""
    tmp_path = f"{UPDATE_CONFIG_PATH}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, UPDATE_CONFIG_PATH)


async def _render_restart_panel(query) -> None:
    """Render the restart-control panel onto an already-answered callback query."""
    config = _read_restart_config()
    enabled = config.get("enabled", True)

    text = (
        "🔄 <b>Restart Control</b>\n\n"
        f"Daily auto-restart: {'🟢 enabled' if enabled else '🔴 disabled'}\n"
        f"Time: {config.get('restart_hour', 3):02d}:{config.get('restart_minute', 0):02d} "
        f"(±{config.get('jitter_minutes', 30)}min jitter)\n\n"
        "To change the time/jitter: <code>/setrestart &lt;hour&gt; &lt;minute&gt; &lt;jitter&gt;</code>\n"
        "e.g. <code>/setrestart 3 0 30</code>"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔴 Disable auto-restart" if enabled else "🟢 Enable auto-restart",
            callback_data="admin_restart_toggle",
        )],
        [InlineKeyboardButton("⚠️ Restart Now", callback_data="admin_restart_now_confirm")],
        [InlineKeyboardButton("◀️ Back", callback_data="admin_back")],
    ])

    await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")


async def admin_restart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the auto-restart schedule and restart controls."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()
    await _render_restart_panel(query)


async def admin_restart_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle the enabled flag for the auto-restart schedule."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return

    config = _read_restart_config()
    config["enabled"] = not config.get("enabled", True)
    try:
        _write_restart_config(config)
    except Exception as e:
        await query.answer(f"Failed to save: {e}", show_alert=True)
        return

    await query.answer("✅ Saved")
    await _render_restart_panel(query)


async def admin_restart_now_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for confirmation before triggering a live restart."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, restart now", callback_data="admin_restart_now"),
         InlineKeyboardButton("✖️ Cancel", callback_data="admin_restart")],
    ])
    await query.edit_message_text(
        "⚠️ This restarts the live bot + web server for all users right now. Continue?",
        reply_markup=kb,
    )


async def admin_restart_now_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger an immediate git pull + restart via the on-disk trigger file."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔", show_alert=True)
        return

    try:
        with open(RESTART_TRIGGER_PATH, "w") as f:
            f.write(f"triggered_by={query.from_user.id}\ntimestamp={datetime.utcnow().isoformat()}\n")
    except Exception as e:
        await query.answer(f"Failed to trigger: {e}", show_alert=True)
        return

    await query.answer()
    await query.edit_message_text("🔄 Restart triggered. Bot will restart in ~10 seconds.")


async def setrestart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the auto-restart schedule. Command: /setrestart <hour> <minute> <jitter_minutes>"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return

    if len(context.args) != 3:
        await update.message.reply_text("Usage: /setrestart <hour 0-23> <minute 0-59> <jitter_minutes 0-120>")
        return

    try:
        hour, minute, jitter = (int(x) for x in context.args)
    except ValueError:
        await update.message.reply_text("❌ All three values must be numbers.")
        return

    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= jitter <= 120):
        await update.message.reply_text("❌ Hour must be 0-23, minute 0-59, jitter 0-120.")
        return

    config = _read_restart_config()
    config.update({"restart_hour": hour, "restart_minute": minute, "jitter_minutes": jitter})
    try:
        _write_restart_config(config)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to save: {e}")
        return

    await update.message.reply_text(
        f"✅ Auto-restart set to {hour:02d}:{minute:02d} (±{jitter}min jitter)."
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
        CommandHandler("setrestart", setrestart_command),
        CallbackQueryHandler(admin_stats_callback, pattern=r"^admin_stats$"),
        CallbackQueryHandler(admin_gw_stats_callback, pattern=r"^admin_gw_stats$"),
        CallbackQueryHandler(admin_ct_stats_callback, pattern=r"^admin_ct_stats$"),
        CallbackQueryHandler(admin_user_stats_callback, pattern=r"^admin_user_stats$"),
        CallbackQueryHandler(admin_users_count_callback, pattern=r"^admin_users_count$"),
        CallbackQueryHandler(admin_blacklist_callback, pattern=r"^admin_blacklist$"),
        CallbackQueryHandler(admin_flags_callback, pattern=r"^admin_flags$"),
        CallbackQueryHandler(admin_referrals_callback, pattern=r"^admin_referrals$"),
        CallbackQueryHandler(admin_feedback_callback, pattern=r"^admin_feedback$"),
        CallbackQueryHandler(admin_dbinfo_callback, pattern=r"^admin_dbinfo$"),
        CallbackQueryHandler(admin_gw_manage_callback, pattern=r"^admin_gw_manage$"),
        CallbackQueryHandler(admin_draw_callback, pattern=r"^admin_draw_\d+$"),
        CallbackQueryHandler(admin_cancel_callback, pattern=r"^admin_cancel_\d+$"),
        CallbackQueryHandler(admin_restart_callback, pattern=r"^admin_restart$"),
        CallbackQueryHandler(admin_restart_toggle_callback, pattern=r"^admin_restart_toggle$"),
        CallbackQueryHandler(admin_restart_now_confirm_callback, pattern=r"^admin_restart_now_confirm$"),
        CallbackQueryHandler(admin_restart_now_callback, pattern=r"^admin_restart_now$"),
        CallbackQueryHandler(admin_back_callback, pattern=r"^admin_back$"),
    ]
