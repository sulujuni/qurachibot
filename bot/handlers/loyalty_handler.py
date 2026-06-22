"""Loyalty points and leaderboard handlers."""

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.i18n import get_text
from bot.utils.lang import get_user_lang
from bot.utils.loyalty import (
    get_or_create_loyalty,
    get_leaderboard,
    get_user_rank,
    spend_points,
    POINTS_CONFIG,
)


async def points_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's loyalty points. Command: /points"""
    user = update.effective_user
    lang = await get_user_lang(user.id)
    loyalty = await get_or_create_loyalty(user.id, user.username, user.first_name)
    rank = await get_user_rank(user.id)

    text = (
        f"💎 <b>Your Loyalty Points</b>\n\n"
        f"⭐ Points: <b>{loyalty.points}</b>\n"
        f"📊 Total earned: {loyalty.total_earned}\n"
        f"💸 Total spent: {loyalty.total_spent}\n"
        f"🏆 Rank: #{rank}\n\n"
        f"<b>Stats:</b>\n"
        f"  🎉 Giveaways joined: {loyalty.giveaways_joined}\n"
        f"  🏅 Contests joined: {loyalty.contests_joined}\n"
        f"  🏆 Wins: {loyalty.wins}\n"
        f"  👥 Referrals: {loyalty.referrals_made}\n\n"
        f"<b>How to earn:</b>\n"
        f"  Join giveaway: +{POINTS_CONFIG['join_giveaway']} pts\n"
        f"  Submit to contest: +{POINTS_CONFIG['submit_entry']} pts\n"
        f"  Vote: +{POINTS_CONFIG['vote']} pts\n"
        f"  Win: +{POINTS_CONFIG['win_giveaway']}-{POINTS_CONFIG['win_contest']} pts\n"
        f"  Refer a friend: +{POINTS_CONFIG['referral']} pts\n\n"
        f"💡 Spend {POINTS_CONFIG['extra_entry_cost']} pts for an extra giveaway entry:\n"
        f"  <code>/redeem &lt;giveaway_id&gt;</code>"
    )

    await update.message.reply_text(text, parse_mode="HTML")


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the leaderboard. Command: /leaderboard"""
    user = update.effective_user
    lang = await get_user_lang(user.id)

    top_users = await get_leaderboard(limit=15)
    user_rank = await get_user_rank(user.id)

    if not top_users:
        await update.message.reply_text("📊 No leaderboard data yet. Start participating!")
        return

    text = "🏆 <b>LEADERBOARD — Top 15</b>\n\n"

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, u in enumerate(top_users, 1):
        medal = medals.get(i, f"{i}.")
        name = f"@{u.username}" if u.username else (u.first_name or f"User {u.user_id}")
        highlight = " ← you" if u.user_id == user.id else ""
        text += f"{medal} {name} — <b>{u.total_earned}</b> pts (🏆{u.wins}){highlight}\n"

    text += f"\n📍 Your rank: <b>#{user_rank}</b>"

    await update.message.reply_text(text, parse_mode="HTML")


async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Redeem points for extra giveaway entry. Command: /redeem <giveaway_id>"""
    user = update.effective_user
    lang = await get_user_lang(user.id)

    if not context.args:
        await update.message.reply_text(
            f"Usage: /redeem <giveaway_id>\n"
            f"Cost: {POINTS_CONFIG['extra_entry_cost']} points per extra entry."
        )
        return

    try:
        giveaway_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid giveaway ID.")
        return

    cost = POINTS_CONFIG["extra_entry_cost"]
    success = await spend_points(
        user.id, cost, "extra_entry",
        description=f"Extra entry for giveaway #{giveaway_id}"
    )

    if success:
        await update.message.reply_text(
            f"✅ <b>Extra entry purchased!</b>\n\n"
            f"You spent {cost} points for an additional entry in giveaway #{giveaway_id}.\n"
            f"Your chances of winning are now higher! 🍀",
            parse_mode="HTML",
        )
    else:
        loyalty = await get_or_create_loyalty(user.id)
        await update.message.reply_text(
            f"❌ Not enough points!\n\n"
            f"Cost: {cost} pts | Your balance: {loyalty.points} pts\n"
            f"Keep participating to earn more! 💪"
        )


def get_loyalty_handlers() -> list:
    """Return loyalty/leaderboard handlers."""
    return [
        CommandHandler("points", points_command),
        CommandHandler("leaderboard", leaderboard_command),
        CommandHandler("redeem", redeem_command),
    ]
