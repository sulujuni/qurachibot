"""Contest command handlers with i18n support."""

from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.i18n import get_text
from bot.models import (
    Contest,
    ContestStatus,
    ContestSubmission,
    ContestType,
    ContestVote,
    async_session,
)
from bot.utils.lang import get_user_lang, t

# Conversation states
C_TITLE, C_DESCRIPTION, C_TYPE, C_PRIZE, C_MAX_SUBMISSIONS = range(5)



# ─── Create Contest ──────────────────────────────────────────────────────────────


async def new_contest_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start contest creation. Command: /newcontest"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    context.user_data["lang"] = lang
    text = get_text("ct_create_title", lang=lang)
    await update.message.reply_text(text, parse_mode="HTML")
    return C_TITLE


async def contest_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("lang", "en")
    context.user_data["contest_title"] = update.message.text.strip()
    text = get_text("ct_create_description", lang=lang)
    await update.message.reply_text(text, parse_mode="HTML")
    return C_DESCRIPTION


async def contest_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("lang", "en")
    text = update.message.text.strip()
    if text.lower() == "/skip":
        context.user_data["contest_description"] = None
    else:
        context.user_data["contest_description"] = text

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(get_text("ct_type_text", lang=lang), callback_data="ctype_text"),
            InlineKeyboardButton(get_text("ct_type_photo", lang=lang), callback_data="ctype_photo"),
        ],
        [InlineKeyboardButton(get_text("ct_type_any", lang=lang), callback_data="ctype_any")],
    ])
    msg = get_text("ct_create_type", lang=lang)
    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="HTML")
    return C_TYPE



async def contest_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get("lang", "en")
    type_map = {"ctype_text": ContestType.TEXT, "ctype_photo": ContestType.PHOTO, "ctype_any": ContestType.ANY}
    context.user_data["contest_type"] = type_map[query.data]
    msg = get_text("ct_create_prize", lang=lang)
    await query.edit_message_text(msg, parse_mode="HTML")
    return C_PRIZE


async def contest_prize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("lang", "en")
    text = update.message.text.strip()
    if text.lower() == "/skip":
        context.user_data["contest_prize"] = None
    else:
        context.user_data["contest_prize"] = text
    msg = get_text("ct_create_max_subs", lang=lang)
    await update.message.reply_text(msg, parse_mode="HTML")
    return C_MAX_SUBMISSIONS


async def contest_max_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("lang", "en")
    try:
        max_subs = int(update.message.text.strip())
        if max_subs < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text(get_text("ct_invalid_number", lang=lang))
        return C_MAX_SUBMISSIONS

    async with async_session() as session:
        contest = Contest(
            title=context.user_data["contest_title"],
            description=context.user_data.get("contest_description"),
            prize=context.user_data.get("contest_prize"),
            contest_type=context.user_data["contest_type"],
            max_submissions_per_user=max_subs,
            creator_id=update.effective_user.id,
            creator_username=update.effective_user.username,
            chat_id=update.effective_chat.id,
        )
        session.add(contest)
        await session.commit()
        await session.refresh(contest)

    type_key = f"ct_type_{contest.contest_type.value}"
    prize_text = f"\n🎁 {get_text('ct_create_prize', lang=lang).split(chr(10))[0]}: <b>{contest.prize}</b>" if contest.prize else ""
    desc_text = f"\n📝 {contest.description}" if contest.description else ""

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(get_text("ct_submit_button", lang=lang), callback_data=f"submit_contest_{contest.id}")],
        [InlineKeyboardButton(get_text("ct_view_button", lang=lang), callback_data=f"view_contest_{contest.id}")],
    ])

    announcement = get_text(
        "ct_announcement", lang=lang,
        title=contest.title, description=desc_text,
        type=get_text(type_key, lang=lang), max_subs=max_subs,
        prize=prize_text, status=get_text("ct_status_accepting", lang=lang),
        sub_count=0, id=contest.id,
    )
    await update.message.reply_text(announcement, reply_markup=keyboard, parse_mode="HTML")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_contest_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = await t("ct_cancelled", user_id)
    context.user_data.clear()
    await update.message.reply_text(text)
    return ConversationHandler.END



# ─── Submit to Contest ───────────────────────────────────────────────────────────


async def submit_contest_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    contest_id = int(query.data.split("_")[-1])
    lang = await get_user_lang(query.from_user.id)

    async with async_session() as session:
        result = await session.execute(select(Contest).where(Contest.id == contest_id))
        contest = result.scalar_one_or_none()

    if not contest:
        await query.answer(get_text("ct_not_found", lang=lang), show_alert=True)
        return
    if contest.status != ContestStatus.ACCEPTING_SUBMISSIONS:
        await query.answer(get_text("ct_not_accepting", lang=lang), show_alert=True)
        return

    await query.answer()
    text = get_text("ct_submit_prompt", lang=lang, title=contest.title, id=contest.id, type=contest.contest_type.value)
    await query.message.reply_text(text, parse_mode="HTML")


async def submit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Submit an entry. Command: /submit <contest_id> [text]"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    if not context.args:
        await update.message.reply_text(get_text("ct_submit_usage", lang=lang), parse_mode="HTML")
        return

    try:
        contest_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(get_text("ct_invalid_id", lang=lang))
        return

    user = update.effective_user
    text_content = " ".join(context.args[1:]) if len(context.args) > 1 else None
    file_id = None
    caption = None

    if update.message.reply_to_message and update.message.reply_to_message.photo:
        file_id = update.message.reply_to_message.photo[-1].file_id
        caption = update.message.reply_to_message.caption
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        caption = update.message.caption

    async with async_session() as session:
        result = await session.execute(select(Contest).where(Contest.id == contest_id))
        contest = result.scalar_one_or_none()

        if not contest:
            await update.message.reply_text(get_text("ct_not_found", lang=lang))
            return
        if contest.status != ContestStatus.ACCEPTING_SUBMISSIONS:
            await update.message.reply_text(get_text("ct_not_accepting", lang=lang))
            return
        if contest.contest_type == ContestType.TEXT and not text_content:
            await update.message.reply_text(get_text("ct_requires_text", lang=lang))
            return
        if contest.contest_type == ContestType.PHOTO and not file_id:
            await update.message.reply_text(get_text("ct_requires_photo", lang=lang))
            return
        if not text_content and not file_id:
            await update.message.reply_text(get_text("ct_requires_content", lang=lang))
            return

        # Check limit
        result = await session.execute(
            select(func.count(ContestSubmission.id)).where(
                ContestSubmission.contest_id == contest_id,
                ContestSubmission.user_id == user.id,
            )
        )
        count = result.scalar()
        if count >= contest.max_submissions_per_user:
            await update.message.reply_text(get_text("ct_max_reached", lang=lang, max=contest.max_submissions_per_user))
            return

        submission = ContestSubmission(
            contest_id=contest_id, user_id=user.id,
            username=user.username, first_name=user.first_name,
            text_content=text_content, file_id=file_id, caption=caption,
        )
        session.add(submission)
        await session.commit()
        await session.refresh(submission)

    text = get_text("ct_submitted", lang=lang, title=contest.title, id=submission.id)
    await update.message.reply_text(text, parse_mode="HTML")



# ─── View Submissions ────────────────────────────────────────────────────────────


async def view_contest_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    contest_id = int(query.data.split("_")[-1])
    lang = await get_user_lang(query.from_user.id)
    await query.answer()
    await _show_submissions(query.message, contest_id, lang)


async def view_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View submissions. Command: /submissions <contest_id>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    if not context.args:
        await update.message.reply_text(get_text("ct_vote_usage", lang=lang), parse_mode="HTML")
        return
    try:
        contest_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(get_text("ct_invalid_id", lang=lang))
        return
    await _show_submissions(update.message, contest_id, lang)


async def _show_submissions(message, contest_id: int, lang: str) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Contest)
            .options(selectinload(Contest.submissions).selectinload(ContestSubmission.votes))
            .where(Contest.id == contest_id)
        )
        contest = result.scalar_one_or_none()

    if not contest:
        await message.reply_text(get_text("ct_not_found", lang=lang))
        return
    if not contest.submissions:
        await message.reply_text(get_text("ct_no_submissions", lang=lang, title=contest.title), parse_mode="HTML")
        return

    text = get_text("ct_submissions_header", lang=lang, title=contest.title)
    sorted_subs = sorted(contest.submissions, key=lambda s: len(s.votes), reverse=True)
    votes_word = get_text("votes", lang=lang)

    for i, sub in enumerate(sorted_subs[:20], 1):
        user_display = f"@{sub.username}" if sub.username else (sub.first_name or f"User {sub.user_id}")
        v = len(sub.votes)
        if sub.text_content:
            preview = sub.text_content[:50] + ("..." if len(sub.text_content) > 50 else "")
            text += f"{i}. {user_display} — \"{preview}\"\n   👍 {v} {votes_word} (ID: {sub.id})\n\n"
        else:
            text += f"{i}. {user_display} — [Photo]\n   👍 {v} {votes_word} (ID: {sub.id})\n\n"

    if contest.status in (ContestStatus.ACCEPTING_SUBMISSIONS, ContestStatus.VOTING):
        text += get_text("ct_vote_hint", lang=lang)

    await message.reply_text(text, parse_mode="HTML")



# ─── Voting ──────────────────────────────────────────────────────────────────────


async def vote_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Vote for a submission. Command: /vote <submission_id>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    if not context.args:
        await update.message.reply_text(get_text("ct_vote_usage", lang=lang), parse_mode="HTML")
        return
    try:
        submission_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(get_text("ct_sub_not_found", lang=lang))
        return

    async with async_session() as session:
        result = await session.execute(
            select(ContestSubmission).options(selectinload(ContestSubmission.contest)).where(ContestSubmission.id == submission_id)
        )
        submission = result.scalar_one_or_none()
        if not submission:
            await update.message.reply_text(get_text("ct_sub_not_found", lang=lang))
            return

        contest = submission.contest
        if contest.status not in (ContestStatus.ACCEPTING_SUBMISSIONS, ContestStatus.VOTING):
            await update.message.reply_text(get_text("ct_voting_closed", lang=lang))
            return
        if submission.user_id == user_id:
            await update.message.reply_text(get_text("ct_cant_vote_self", lang=lang))
            return

        existing = await session.execute(
            select(ContestVote).where(ContestVote.submission_id == submission_id, ContestVote.user_id == user_id)
        )
        if existing.scalar_one_or_none():
            await update.message.reply_text(get_text("ct_already_voted", lang=lang))
            return

        vote = ContestVote(submission_id=submission_id, user_id=user_id)
        session.add(vote)
        submission.vote_count += 1
        await session.commit()

    submitter = f"@{submission.username}" if submission.username else (submission.first_name or "Unknown")
    text = get_text("ct_vote_recorded", lang=lang, user=submitter, title=contest.title)
    await update.message.reply_text(text, parse_mode="HTML")



# ─── Start Voting / End Contest / Cancel ─────────────────────────────────────────


async def start_voting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Move to voting phase. Command: /startvoting <contest_id>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    if not context.args:
        await update.message.reply_text(get_text("ct_startvoting_usage", lang=lang), parse_mode="HTML")
        return
    try:
        contest_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(get_text("ct_invalid_id", lang=lang))
        return

    async with async_session() as session:
        result = await session.execute(select(Contest).where(Contest.id == contest_id))
        contest = result.scalar_one_or_none()
        if not contest:
            await update.message.reply_text(get_text("ct_not_found", lang=lang))
            return
        if contest.creator_id != user_id:
            await update.message.reply_text(get_text("ct_only_creator", lang=lang))
            return
        if contest.status != ContestStatus.ACCEPTING_SUBMISSIONS:
            await update.message.reply_text(get_text("ct_not_in_submissions", lang=lang))
            return
        contest.status = ContestStatus.VOTING
        contest.submissions_end_at = datetime.utcnow()
        await session.commit()

    text = get_text("ct_voting_started", lang=lang, title=contest.title, id=contest_id)
    await update.message.reply_text(text, parse_mode="HTML")


async def end_contest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """End contest and announce winners. Command: /endcontest <contest_id>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    if not context.args:
        await update.message.reply_text(get_text("ct_end_usage", lang=lang), parse_mode="HTML")
        return
    try:
        contest_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(get_text("ct_invalid_id", lang=lang))
        return

    async with async_session() as session:
        result = await session.execute(
            select(Contest).options(selectinload(Contest.submissions).selectinload(ContestSubmission.votes)).where(Contest.id == contest_id)
        )
        contest = result.scalar_one_or_none()
        if not contest:
            await update.message.reply_text(get_text("ct_not_found", lang=lang))
            return
        if contest.creator_id != user_id:
            await update.message.reply_text(get_text("ct_only_creator_end", lang=lang))
            return
        if contest.status == ContestStatus.COMPLETED:
            await update.message.reply_text(get_text("ct_already_ended", lang=lang))
            return
        if contest.status == ContestStatus.CANCELLED:
            await update.message.reply_text(get_text("ct_was_cancelled", lang=lang))
            return
        if not contest.submissions:
            await update.message.reply_text(get_text("ct_no_subs_to_judge", lang=lang))
            return

        sorted_subs = sorted(contest.submissions, key=lambda s: len(s.votes), reverse=True)
        winners = sorted_subs[:contest.winner_count]
        contest.status = ContestStatus.COMPLETED
        contest.completed_at = datetime.utcnow()
        await session.commit()

    votes_word = get_text("votes", lang=lang)
    winners_text = "\n".join(
        f"🏆 {i+1}. {('@' + w.username) if w.username else (w.first_name or f'User {w.user_id}')} — {len(w.votes)} {votes_word}"
        for i, w in enumerate(winners)
    )
    prize_text = f"\n🎁 {contest.prize}" if contest.prize else ""
    text = get_text("ct_results", lang=lang, title=contest.title, total=len(contest.submissions), prize=prize_text, winners=winners_text)
    await update.message.reply_text(text, parse_mode="HTML")



async def cancel_contest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a contest. Command: /cancelcontest <contest_id>"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)
    if not context.args:
        await update.message.reply_text(get_text("ct_cancel_usage", lang=lang), parse_mode="HTML")
        return
    try:
        contest_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(get_text("ct_invalid_id", lang=lang))
        return

    async with async_session() as session:
        result = await session.execute(select(Contest).where(Contest.id == contest_id))
        contest = result.scalar_one_or_none()
        if not contest:
            await update.message.reply_text(get_text("ct_not_found", lang=lang))
            return
        if contest.creator_id != user_id:
            await update.message.reply_text(get_text("ct_cancel_only_creator", lang=lang))
            return
        if contest.status in (ContestStatus.COMPLETED, ContestStatus.CANCELLED):
            await update.message.reply_text(get_text("ct_cancel_already_done", lang=lang))
            return
        contest.status = ContestStatus.CANCELLED
        await session.commit()

    text = get_text("ct_cancel_done", lang=lang, title=contest.title)
    await update.message.reply_text(text, parse_mode="HTML")


async def my_contests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List user's contests. Command: /mycontests"""
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    async with async_session() as session:
        result = await session.execute(
            select(Contest).options(selectinload(Contest.submissions))
            .where(Contest.creator_id == user_id).order_by(Contest.created_at.desc()).limit(10)
        )
        contests = result.scalars().all()

    if not contests:
        await update.message.reply_text(get_text("ct_my_list_empty", lang=lang))
        return

    status_emoji = {
        ContestStatus.ACCEPTING_SUBMISSIONS: "📤",
        ContestStatus.VOTING: "🗳",
        ContestStatus.COMPLETED: "✅",
        ContestStatus.CANCELLED: "❌",
    }

    text = get_text("ct_my_list_header", lang=lang)
    for c in contests:
        emoji = status_emoji.get(c.status, "❓")
        text += f"{emoji} <b>{c.title}</b> (ID: {c.id})\n   📤 {len(c.submissions)} | {c.status.value}\n\n"

    await update.message.reply_text(text, parse_mode="HTML")



# ─── Handler Registration ────────────────────────────────────────────────────────


def get_contest_handlers() -> list:
    """Return all contest-related handlers."""
    create_conv = ConversationHandler(
        entry_points=[CommandHandler("newcontest", new_contest_start)],
        states={
            C_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, contest_title)],
            C_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, contest_description),
                CommandHandler("skip", contest_description),
            ],
            C_TYPE: [CallbackQueryHandler(contest_type_selected, pattern=r"^ctype_")],
            C_PRIZE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, contest_prize),
                CommandHandler("skip", contest_prize),
            ],
            C_MAX_SUBMISSIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, contest_max_submissions)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_contest_creation)],
    )

    return [
        create_conv,
        CommandHandler("submit", submit_entry),
        CommandHandler("submissions", view_submissions),
        CommandHandler("vote", vote_submission),
        CommandHandler("startvoting", start_voting),
        CommandHandler("endcontest", end_contest),
        CommandHandler("cancelcontest", cancel_contest),
        CommandHandler("mycontests", my_contests),
        CallbackQueryHandler(submit_contest_callback, pattern=r"^submit_contest_\\d+$"),
        CallbackQueryHandler(view_contest_callback, pattern=r"^view_contest_\\d+$"),
    ]
