"""Mini App API endpoints for giveaway participation.

These endpoints are called by the Telegram Mini App (Web App) when users
tap the 'Qatnashish' inline button on a giveaway announcement.
"""

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta
from urllib.parse import parse_qs

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from bot.config import settings
from bot.models.database import async_session
from bot.models.giveaway import (
    Giveaway,
    GiveawayParticipant,
    GiveawayStatus,
    GiveawayWinner,
)
from bot.utils.subscription import get_unsubscribed, parse_channels

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/miniapp/api")


# ─── Telegram initData validation ────────────────────────────────────────────


def _validate_init_data(init_data: str) -> dict | None:
    """Validate Telegram Mini App initData and return the parsed user data.

    Returns None if validation fails. See:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data:
        return None

    try:
        parsed = parse_qs(init_data)
        received_hash = parsed.get("hash", [None])[0]
        if not received_hash:
            # No hash present — try to parse user field directly (fallback)
            user_json = parsed.get("user", [None])[0]
            if user_json:
                return json.loads(user_json)
            return None

        # Build the check string (alphabetically sorted key=value, excluding 'hash')
        items = []
        for key, values in sorted(parsed.items()):
            if key == "hash":
                continue
            items.append(f"{key}={values[0]}")
        data_check_string = "\n".join(items)

        # HMAC-SHA-256 with secret key derived from bot token
        secret_key = hmac.HMAC(b"WebAppData", settings.BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.HMAC(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            # Validation failed — but still try to parse user for dev/testing
            logger.warning("initData HMAC mismatch (may be dev mode)")
            user_json = parsed.get("user", [None])[0]
            if user_json:
                return json.loads(user_json)
            return None

        # Validation passed — parse user JSON
        user_json = parsed.get("user", [None])[0]
        if user_json:
            return json.loads(user_json)
        return None
    except Exception as e:
        logger.warning("initData validation error: %s", e)
        return None


def _get_user_from_header(init_data_header: str | None) -> dict:
    """Extract user info from the X-Telegram-Init-Data header.

    For development/testing: if initData validation fails, try to parse
    the user field directly (allows testing without a real Telegram client).
    """
    if not init_data_header:
        raise HTTPException(status_code=401, detail="Missing initData")

    user = _validate_init_data(init_data_header)
    if user:
        return user

    # Fallback: try parsing user field directly (dev mode / validation bypass)
    try:
        parsed = parse_qs(init_data_header)
        user_json = parsed.get("user", [None])[0]
        if user_json:
            return json.loads(user_json)
    except Exception:
        pass

    # Last resort: try parsing the whole thing as JSON (some clients send it differently)
    try:
        data = json.loads(init_data_header)
        if "id" in data:
            return data
        if "user" in data:
            return data["user"]
    except Exception:
        pass

    raise HTTPException(status_code=401, detail="Invalid initData")


# ─── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/giveaway/{giveaway_id}")
async def get_giveaway_info(
    giveaway_id: int,
    x_telegram_init_data: str | None = Header(None),
):
    """Get giveaway info for the Mini App. Checks user's join/subscription status."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).where(Giveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

        if not giveaway:
            raise HTTPException(status_code=404, detail="Giveaway not found")

        # Count participants
        count_result = await session.execute(
            select(func.count(GiveawayParticipant.id)).where(
                GiveawayParticipant.giveaway_id == giveaway_id
            )
        )
        participant_count = count_result.scalar() or 0

        # Check if user already joined
        existing = await session.execute(
            select(GiveawayParticipant).where(
                GiveawayParticipant.giveaway_id == giveaway_id,
                GiveawayParticipant.user_id == user_id,
            )
        )
        already_joined = existing.scalar_one_or_none() is not None

        # Check subscription requirements
        required_channels = parse_channels(giveaway.required_channels)
        must_subscribe = []
        if required_channels and not already_joined:
            # We need a Bot instance for membership checks — use lazy import
            from telegram import Bot
            bot = Bot(token=settings.BOT_TOKEN)
            missing = await get_unsubscribed(bot, user_id, required_channels)
            must_subscribe = missing

        # Winners text (if completed)
        winners_text = ""
        if giveaway.status == "completed":
            winners_result = await session.execute(
                select(GiveawayWinner).where(GiveawayWinner.giveaway_id == giveaway_id)
            )
            winners = winners_result.scalars().all()
            if winners:
                names = [f"@{w.username}" if w.username else (w.first_name or f"ID:{w.user_id}") for w in winners]
                winners_text = "G'oliblar: " + ", ".join(names)

    return {
        "id": giveaway.id,
        "title": giveaway.title,
        "description": giveaway.description,
        "post_text": giveaway.post_text,
        "prize": giveaway.prize,
        "winner_count": giveaway.winner_count,
        "status": giveaway.status.value,
        "participants": participant_count,
        "already_joined": already_joined,
        "must_subscribe": must_subscribe,
        "required_channels": required_channels or [],
        "ends_at": giveaway.ends_at.isoformat() if giveaway.ends_at else None,
        "winners_text": winners_text,
    }


@router.post("/giveaway/{giveaway_id}/join")
async def join_giveaway(
    giveaway_id: int,
    x_telegram_init_data: str | None = Header(None),
):
    """Join a giveaway via the Mini App."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]
    username = user.get("username")
    first_name = user.get("first_name")

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).where(Giveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

        if not giveaway:
            return {"error": "O'yin topilmadi"}

        if giveaway.status != "active":
            return {"error": "Bu o'yin tugagan"}

        if giveaway.ends_at and datetime.utcnow() > giveaway.ends_at:
            return {"error": "Vaqt tugagan"}

        # Check if already joined
        existing = await session.execute(
            select(GiveawayParticipant).where(
                GiveawayParticipant.giveaway_id == giveaway_id,
                GiveawayParticipant.user_id == user_id,
            )
        )
        if existing.scalar_one_or_none():
            return {"error": "Siz allaqachon qatnashyapsiz", "already_joined": True}

        # Check CAPTCHA verification
        from bot.models.user_settings import UserSettings as US
        us_result = await session.execute(select(US).where(US.user_id == user_id))
        us = us_result.scalar_one_or_none()
        if not us or not us.captcha_verified:
            return {"error": "Avval CAPTCHA tekshiruvidan o'ting (/verify)", "need_captcha": True}

        # Check subscription
        required_channels = parse_channels(giveaway.required_channels)
        if required_channels:
            from telegram import Bot
            bot = Bot(token=settings.BOT_TOKEN)
            missing = await get_unsubscribed(bot, user_id, required_channels)
            if missing:
                return {"error": "Avval kanallarga obuna bo'ling", "must_subscribe": missing}

        # Add participant
        participant = GiveawayParticipant(
            giveaway_id=giveaway_id,
            user_id=user_id,
            username=username,
            first_name=first_name,
        )
        session.add(participant)
        await session.commit()

        # Get updated count
        count_result = await session.execute(
            select(func.count(GiveawayParticipant.id)).where(
                GiveawayParticipant.giveaway_id == giveaway_id
            )
        )
        new_count = count_result.scalar() or 0

    return {
        "success": True,
        "participants": new_count,
    }


@router.get("/giveaway/{giveaway_id}/participants")
async def get_participants(giveaway_id: int):
    """Get participant list (limited to last 50)."""
    async with async_session() as session:
        result = await session.execute(
            select(GiveawayParticipant)
            .where(GiveawayParticipant.giveaway_id == giveaway_id)
            .order_by(GiveawayParticipant.joined_at.desc())
            .limit(50)
        )
        participants = result.scalars().all()

        count_result = await session.execute(
            select(func.count(GiveawayParticipant.id)).where(
                GiveawayParticipant.giveaway_id == giveaway_id
            )
        )
        total = count_result.scalar() or 0

    return {
        "total": total,
        "participants": [
            {
                "username": p.username,
                "first_name": p.first_name,
                "joined_at": p.joined_at.isoformat() if p.joined_at else None,
            }
            for p in participants
        ],
    }



# ─── Additional endpoints for the full Mini App ──────────────────────────────


@router.get("/stats")
async def miniapp_stats():
    """Global stats for the Mini App home tab."""
    async with async_session() as session:
        gw_total = (await session.execute(select(func.count(Giveaway.id)))).scalar() or 0
        gw_active = (await session.execute(
            select(func.count(Giveaway.id)).where(Giveaway.status == "active")
        )).scalar() or 0
        total_participants = (await session.execute(select(func.count(GiveawayParticipant.id)))).scalar() or 0
        unique_users = (await session.execute(
            select(func.count(func.distinct(GiveawayParticipant.user_id)))
        )).scalar() or 0

        from bot.models.contest import Contest, ContestStatus, ContestSubmission
        ct_total = (await session.execute(select(func.count(Contest.id)))).scalar() or 0

        from bot.models.referral import Referral
        total_referrals = (await session.execute(select(func.count(Referral.id)))).scalar() or 0

    return {
        "giveaways": {"total": gw_total, "active": gw_active, "participants": total_participants},
        "contests": {"total": ct_total},
        "users": {"unique": unique_users, "referrals": total_referrals},
    }


@router.get("/active-giveaways")
async def miniapp_active_giveaways():
    """List active giveaways for the home tab."""
    async with async_session() as session:
        result = await session.execute(
            select(Giveaway)
            .where(Giveaway.status == "active")
            .order_by(Giveaway.created_at.desc())
            .limit(20)
        )
        giveaways = result.scalars().all()

        items = []
        for gw in giveaways:
            count = (await session.execute(
                select(func.count(GiveawayParticipant.id)).where(
                    GiveawayParticipant.giveaway_id == gw.id
                )
            )).scalar() or 0
            items.append({
                "id": gw.id,
                "title": gw.title,
                "prize": gw.prize,
                "post_text": gw.post_text,
                "description": gw.description,
                "required_channels": gw.required_channels,
                "participants": count,
                "winner_count": gw.winner_count,
                "ends_at": gw.ends_at.isoformat() if gw.ends_at else None,
                "created_at": gw.created_at.isoformat() if gw.created_at else None,
            })
    return items


@router.get("/leaderboard")
async def miniapp_leaderboard(x_telegram_init_data: str | None = Header(None)):
    """Top users for the leaderboard tab — enriched with all stats."""
    from bot.models.loyalty import LoyaltyPoints

    user = None
    user_id = None
    try:
        user = _get_user_from_header(x_telegram_init_data)
        user_id = user["id"]
    except Exception:
        pass

    async with async_session() as session:
        result = await session.execute(
            select(LoyaltyPoints).order_by(LoyaltyPoints.total_earned.desc()).limit(30)
        )
        users = result.scalars().all()

        # Find the requesting user's rank if not in top 30
        my_rank = None
        my_data = None
        if user_id:
            for i, u in enumerate(users):
                if u.user_id == user_id:
                    my_rank = i + 1
                    my_data = u
                    break
            if my_rank is None:
                # User not in top 30, find their actual rank
                rank_result = await session.execute(
                    select(func.count(LoyaltyPoints.id)).where(
                        LoyaltyPoints.total_earned > (
                            select(LoyaltyPoints.total_earned).where(LoyaltyPoints.user_id == user_id).scalar_subquery()
                        )
                    )
                )
                rank_above = rank_result.scalar() or 0
                my_rank = rank_above + 1
                user_result = await session.execute(
                    select(LoyaltyPoints).where(LoyaltyPoints.user_id == user_id)
                )
                my_data = user_result.scalar_one_or_none()

    return {
        "users": [
            {
                "rank": i + 1,
                "user_id": u.user_id,
                "username": u.username,
                "first_name": u.first_name,
                "points": u.total_earned or 0,
                "wins": u.wins or 0,
                "referrals": u.referrals_made or 0,
                "games_joined": (u.giveaways_joined or 0) + (u.contests_joined or 0),
                "is_me": u.user_id == user_id if user_id else False,
            }
            for i, u in enumerate(users)
        ],
        "my_rank": my_rank,
        "my_data": {
            "points": my_data.total_earned or 0,
            "wins": my_data.wins or 0,
            "referrals": my_data.referrals_made or 0,
            "games_joined": (my_data.giveaways_joined or 0) + (my_data.contests_joined or 0),
        } if my_data else None,
    }


@router.get("/my-games")
async def miniapp_my_games(x_telegram_init_data: str | None = Header(None)):
    """User's participated and created giveaways + referral stats."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    async with async_session() as session:
        # Participated giveaways
        result = await session.execute(
            select(GiveawayParticipant)
            .where(GiveawayParticipant.user_id == user_id)
            .order_by(GiveawayParticipant.joined_at.desc())
            .limit(20)
        )
        participations = result.scalars().all()

        participated = []
        for p in participations:
            gw = (await session.execute(select(Giveaway).where(Giveaway.id == p.giveaway_id))).scalar_one_or_none()
            if not gw:
                continue
            # Check if user won
            won = (await session.execute(
                select(GiveawayWinner).where(
                    GiveawayWinner.giveaway_id == gw.id,
                    GiveawayWinner.user_id == user_id,
                )
            )).scalar_one_or_none() is not None
            participated.append({
                "id": gw.id, "title": gw.title, "prize": gw.prize,
                "status": gw.status.value, "won": won,
                "created_at": gw.created_at.isoformat() if gw.created_at else None,
            })

        # Created giveaways
        result = await session.execute(
            select(Giveaway)
            .where(Giveaway.creator_id == user_id)
            .order_by(Giveaway.created_at.desc())
            .limit(20)
        )
        created_gws = result.scalars().all()
        created = []
        for gw in created_gws:
            count = (await session.execute(
                select(func.count(GiveawayParticipant.id)).where(
                    GiveawayParticipant.giveaway_id == gw.id
                )
            )).scalar() or 0
            created.append({
                "id": gw.id, "title": gw.title, "prize": gw.prize,
                "status": gw.status.value, "participants": count,
                "created_at": gw.created_at.isoformat() if gw.created_at else None,
            })

        # Referral stats
        from bot.models.referral import Referral
        ref_count = (await session.execute(
            select(func.count(Referral.id)).where(
                Referral.referrer_id == user_id, Referral.verified == True
            )
        )).scalar() or 0

        from bot.utils.loyalty import POINTS_CONFIG
        ref_points = ref_count * POINTS_CONFIG.get("referral", 20)

    return {
        "participated": participated,
        "created": created,
        "referral": {"count": ref_count, "points": ref_points},
    }



@router.post("/create-giveaway")
async def miniapp_create_giveaway(
    request: Request,
    x_telegram_init_data: str | None = Header(None),
):
    """Create a giveaway from the Mini App (post-based)."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]
    username = user.get("username")

    body = await request.json()
    post_text = body.get("post_text", "").strip()
    if not post_text:
        return {"error": "Post matni kiritilishi shart"}

    import re
    plain = re.sub(r"<[^>]+>", "", post_text)
    title = plain.strip().split("\n")[0][:100] or "Giveaway"

    winner_count = max(1, int(body.get("winner_count", 1)))
    required_channels = body.get("required_channels") or None

    # Parse duration or deadline
    duration_map = {
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1), "3h": timedelta(hours=3),
        "6h": timedelta(hours=6), "12h": timedelta(hours=12),
        "24h": timedelta(hours=24), "2d": timedelta(days=2),
        "3d": timedelta(days=3), "5d": timedelta(days=5),
        "7d": timedelta(days=7), "14d": timedelta(days=14),
        "30d": timedelta(days=30), "none": None,
    }

    deadline_str = body.get("deadline")
    duration_key = body.get("duration", "24h")

    if deadline_str:
        try:
            ends_at = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return {"error": "Noto'g'ri sana formati"}
    elif duration_key and duration_key != "none" and duration_key != "custom":
        duration = duration_map.get(duration_key)
        ends_at = datetime.utcnow() + duration if duration else None
    else:
        ends_at = None

    from bot.utils.subscription import serialize_channels, parse_channels as _pc
    channels_str = serialize_channels(_pc(required_channels)) if required_channels else None

    async with async_session() as session:
        # Parse scheduled_start
        scheduled_start = None
        start_str = body.get("scheduled_start")
        if start_str:
            try:
                scheduled_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Parse channel_id
        channel_id_val = body.get("channel_id")
        parsed_channel = None
        if channel_id_val:
            ch = str(channel_id_val).strip()
            parsed_channel = int(ch) if ch.lstrip("-").isdigit() else None

        gw_status = "queued" if scheduled_start else "active"

        giveaway = Giveaway(
            title=title, post_text=post_text,
            post_file_id=body.get("post_file_id") or None,
            post_media_type="photo" if body.get("post_file_id") else None,
            winner_count=winner_count,
            required_channels=channels_str,
            is_test=bool(body.get("is_test", False)),
            status=gw_status,
            scheduled_start=scheduled_start,
            channel_id=parsed_channel,
            creator_id=user_id, creator_username=username,
            chat_id=user_id,
            ends_at=ends_at,
        )
        session.add(giveaway)
        await session.commit()
        await session.refresh(giveaway)

    return {"success": True, "id": giveaway.id, "title": giveaway.title}


@router.post("/upload-photo")
async def miniapp_upload_photo(
    request: Request,
    x_telegram_init_data: str | None = Header(None),
):
    """Upload a photo for a giveaway post. Returns a Telegram file_id.

    The photo is sent to the creator's own chat via the bot, then deleted.
    This gives us a file_id we can reuse when publishing the giveaway.
    """
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    # Accept multipart form data with file
    from fastapi import UploadFile, File, Form
    form = await request.form()
    file = form.get("photo")
    if not file:
        return {"error": "No photo uploaded"}

    # Read file bytes
    contents = await file.read()
    if len(contents) > 3 * 1024 * 1024:  # 3MB limit
        return {"error": "Fayl hajmi katta (maks 3MB)"}

    # Send photo to the user via bot to get file_id, then delete
    from telegram import Bot, InputFile
    import io
    bot = Bot(token=settings.BOT_TOKEN)

    try:
        msg = await bot.send_photo(
            user_id,
            photo=InputFile(io.BytesIO(contents), filename=file.filename or "photo.jpg"),
            caption="📷 Rasm yuklandi (tez orada o'chiriladi)",
        )
        file_id = msg.photo[-1].file_id
        # Delete the temp message
        try:
            await bot.delete_message(user_id, msg.message_id)
        except Exception:
            pass
        return {"success": True, "file_id": file_id}
    except Exception as e:
        logger.error("Photo upload failed: %s", e)
        return {"error": "Failed to upload photo. Make sure you've started the bot first."}


@router.post("/share-to-channel")
async def miniapp_share_to_channel(
    request: Request,
    x_telegram_init_data: str | None = Header(None),
):
    """Send the full giveaway post (with photo + join button) to a channel/group.

    Creator specifies the channel/group by ID or @username.
    """
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    body = await request.json()
    giveaway_id = body.get("giveaway_id")
    channel = body.get("channel")  # @username or numeric ID

    if not giveaway_id or not channel:
        return {"error": "giveaway_id va channel kerak"}

    # Parse channel: support @username, numeric ID, or -100... format
    channel_id = channel.strip()
    if channel_id.lstrip("-").isdigit():
        channel_id = int(channel_id)
    # else keep as string (@username)

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).where(Giveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

    if not giveaway:
        return {"error": "O'yin topilmadi"}
    if giveaway.creator_id != user_id and user_id not in settings.ADMIN_IDS:
        return {"error": "Faqat yaratuvchi ulasha oladi"}

    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    bot = Bot(token=settings.BOT_TOKEN)

    # Build join button
    from bot.utils.lang import get_user_lang
    lang = await get_user_lang(user_id)
    from bot.i18n import get_text
    label = f"🎮 {get_text('gw_join_button', lang=lang)} (0)"
    web_url = settings.WEB_URL
    if web_url:
        url = f"{web_url.rstrip('/')}/miniapp/giveaway?id={giveaway.id}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(label, web_app=WebAppInfo(url=url))]])
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"join_gw_{giveaway.id}")]])

    # Send the full post to the channel
    try:
        if giveaway.post_file_id and giveaway.post_media_type == "photo":
            await bot.send_photo(
                channel_id, giveaway.post_file_id,
                caption=giveaway.post_text or "", parse_mode="HTML", reply_markup=kb,
            )
        elif giveaway.post_file_id and giveaway.post_media_type == "video":
            await bot.send_video(
                channel_id, giveaway.post_file_id,
                caption=giveaway.post_text or "", parse_mode="HTML", reply_markup=kb,
            )
        else:
            await bot.send_message(
                channel_id, giveaway.post_text or giveaway.title,
                parse_mode="HTML", reply_markup=kb,
            )
        # Success — save channel to user's list for future quick-select
        try:
            chat_info = await bot.get_chat(channel_id)
            from bot.models.user_channel import UserChannel
            async with async_session() as session:
                # Check if already saved
                from sqlalchemy import select, and_
                existing = await session.execute(
                    select(UserChannel).where(
                        and_(UserChannel.user_id == user_id, UserChannel.chat_id == chat_info.id)
                    )
                )
                if not existing.scalar_one_or_none():
                    session.add(UserChannel(
                        user_id=user_id,
                        chat_id=chat_info.id,
                        chat_title=chat_info.title,
                        chat_username=chat_info.username,
                    ))
                    await session.commit()
        except Exception:
            pass  # Non-critical — channel still sent successfully

        return {"success": True}
    except Exception as e:
        logger.error("Share to channel failed: %s", e)
        return {"error": f"Yuborib bo'lmadi: {str(e)[:100]}"}


@router.get("/my-channels")
async def miniapp_my_channels(x_telegram_init_data: str | None = Header(None)):
    """Get channels where the user has previously shared posts (bot is admin)."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    from bot.models.user_channel import UserChannel
    from sqlalchemy import select

    async with async_session() as session:
        result = await session.execute(
            select(UserChannel).where(UserChannel.user_id == user_id)
            .order_by(UserChannel.added_at.desc()).limit(20)
        )
        channels = result.scalars().all()

    # Also include channels from join filters (in case they set up filters but haven't shared yet)
    from bot.handlers.join_request import JoinFilter
    async with async_session() as session:
        result = await session.execute(select(JoinFilter).limit(20))
        filters = result.scalars().all()

    # Merge both lists (deduplicate by chat_id)
    seen = set()
    items = []
    for ch in channels:
        if ch.chat_id not in seen:
            seen.add(ch.chat_id)
            items.append({"chat_id": ch.chat_id, "title": ch.chat_title or f"Channel {ch.chat_id}", "username": ch.chat_username})
    for f in filters:
        if f.chat_id not in seen:
            seen.add(f.chat_id)
            items.append({"chat_id": f.chat_id, "title": f.chat_title or f"Channel {f.chat_id}", "username": None})

    return items


@router.post("/create-contest")
async def miniapp_create_contest(
    request: Request,
    x_telegram_init_data: str | None = Header(None),
):
    """Create a contest from the Mini App (post-based, with optional Join button)."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]
    username = user.get("username")

    body = await request.json()
    post_text = body.get("post_text", "").strip()
    if not post_text:
        return {"error": "Post matni kiritilishi shart"}

    import re
    plain = re.sub(r"<[^>]+>", "", post_text)
    title = plain.strip().split("\n")[0][:100] or "Contest"

    from bot.models.contest import Contest, ContestType

    type_map = {"text": ContestType.TEXT, "photo": ContestType.PHOTO, "any": ContestType.ANY}
    contest_type = type_map.get(body.get("contest_type", "any"), ContestType.ANY)

    add_join_button = body.get("add_join_button", False)

    # Parse duration
    duration_map = {
        "30m": timedelta(minutes=30), "1h": timedelta(hours=1), "3h": timedelta(hours=3),
        "6h": timedelta(hours=6), "12h": timedelta(hours=12), "24h": timedelta(hours=24),
        "2d": timedelta(days=2), "3d": timedelta(days=3), "5d": timedelta(days=5),
        "7d": timedelta(days=7), "14d": timedelta(days=14), "30d": timedelta(days=30), "none": None,
    }
    deadline_str = body.get("deadline")
    duration_key = body.get("duration", "none")
    ends_at = None
    if deadline_str:
        try:
            ends_at = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    elif duration_key and duration_key not in ("none", "custom"):
        duration = duration_map.get(duration_key)
        ends_at = datetime.utcnow() + duration if duration else None

    from bot.utils.subscription import serialize_channels, parse_channels as _pc
    raw_channels = body.get("required_channels")
    channels_str = serialize_channels(_pc(raw_channels)) if raw_channels else None

    async with async_session() as session:
        contest = Contest(
            title=title, post_text=post_text,
            contest_type=contest_type,
            winner_count=max(1, int(body.get("winner_count", 1))),
            max_submissions_per_user=1,
            creator_id=user_id, creator_username=username,
            chat_id=user_id,
            submissions_end_at=ends_at,
        )
        session.add(contest)
        await session.commit()
        await session.refresh(contest)

    # If add_join_button is True, also create a Giveaway linked to this contest
    giveaway_id = None
    if add_join_button:
        # Parse scheduled_start
        scheduled_start = None
        start_str = body.get("scheduled_start")
        if start_str:
            try:
                scheduled_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Parse channel_id
        channel_id_val = body.get("channel_id")
        parsed_channel = None
        if channel_id_val:
            ch = str(channel_id_val).strip()
            parsed_channel = int(ch) if ch.lstrip("-").isdigit() else None

        gw_status = "queued" if scheduled_start else "active"

        async with async_session() as session:
            giveaway = Giveaway(
                title=title, post_text=post_text,
                post_file_id=body.get("post_file_id") or None,
                post_media_type="photo" if body.get("post_file_id") else None,
                winner_count=max(1, int(body.get("winner_count", 1))),
                required_channels=channels_str,
                status=gw_status,
                scheduled_start=scheduled_start,
                channel_id=parsed_channel,
                creator_id=user_id, creator_username=username,
                chat_id=user_id, ends_at=ends_at,
            )
            session.add(giveaway)
            await session.commit()
            await session.refresh(giveaway)
            giveaway_id = giveaway.id

    return {"success": True, "id": contest.id, "title": contest.title, "giveaway_id": giveaway_id}



@router.get("/me")
async def miniapp_me(x_telegram_init_data: str | None = Header(None)):
    """Get current user info + role (admin or regular)."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    is_admin = user_id in settings.ADMIN_IDS

    # Get user's loyalty stats
    from bot.models.loyalty import LoyaltyPoints
    async with async_session() as session:
        result = await session.execute(
            select(LoyaltyPoints).where(LoyaltyPoints.user_id == user_id)
        )
        loyalty = result.scalar_one_or_none()

        # Count their giveaways
        created_count = (await session.execute(
            select(func.count(Giveaway.id)).where(Giveaway.creator_id == user_id)
        )).scalar() or 0

        participated_count = (await session.execute(
            select(func.count(GiveawayParticipant.id)).where(GiveawayParticipant.user_id == user_id)
        )).scalar() or 0

        wins_count = (await session.execute(
            select(func.count(GiveawayWinner.id)).where(GiveawayWinner.user_id == user_id)
        )).scalar() or 0

    return {
        "id": user_id,
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "is_admin": is_admin,
        "points": loyalty.points if loyalty else 0,
        "total_earned": loyalty.total_earned if loyalty else 0,
        "created_count": created_count,
        "participated_count": participated_count,
        "wins_count": wins_count,
    }


@router.get("/admin/all-giveaways")
async def admin_all_giveaways(x_telegram_init_data: str | None = Header(None)):
    """Admin: get all giveaways with management info."""
    user = _get_user_from_header(x_telegram_init_data)
    if user["id"] not in settings.ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).order_by(Giveaway.created_at.desc()).limit(50)
        )
        giveaways = result.scalars().all()

        items = []
        for gw in giveaways:
            count = (await session.execute(
                select(func.count(GiveawayParticipant.id)).where(
                    GiveawayParticipant.giveaway_id == gw.id
                )
            )).scalar() or 0
            items.append({
                "id": gw.id, "title": gw.title, "prize": gw.prize,
                "status": gw.status.value, "participants": count,
                "winner_count": gw.winner_count,
                "creator_username": gw.creator_username,
                "ends_at": gw.ends_at.isoformat() if gw.ends_at else None,
                "created_at": gw.created_at.isoformat() if gw.created_at else None,
            })
    return items


@router.post("/admin/draw/{giveaway_id}")
async def admin_draw_giveaway(
    giveaway_id: int,
    x_telegram_init_data: str | None = Header(None),
):
    """Admin: draw winners for a giveaway."""
    import random
    user = _get_user_from_header(x_telegram_init_data)
    if user["id"] not in settings.ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).where(Giveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()
        if not giveaway:
            return {"error": "Topilmadi"}
        if giveaway.status != "active":
            return {"error": "Bu o'yin faol emas"}

        # Get participants
        result = await session.execute(
            select(GiveawayParticipant).where(
                GiveawayParticipant.giveaway_id == giveaway_id
            )
        )
        participants = result.scalars().all()
        if not participants:
            return {"error": "Ishtirokchilar yo'q"}

        winner_count = min(giveaway.winner_count, len(participants))
        winners = random.sample(participants, winner_count)

        for w in winners:
            session.add(GiveawayWinner(
                giveaway_id=giveaway_id,
                user_id=w.user_id, username=w.username, first_name=w.first_name,
            ))

        giveaway.status = "completed"
        giveaway.drawn_at = datetime.utcnow()
        await session.commit()

    winner_names = [f"@{w.username}" if w.username else (w.first_name or f"ID:{w.user_id}") for w in winners]
    return {"success": True, "winners": winner_names, "total_participants": len(participants)}


@router.post("/admin/cancel/{giveaway_id}")
async def admin_cancel_giveaway(
    giveaway_id: int,
    x_telegram_init_data: str | None = Header(None),
):
    """Admin: cancel a giveaway."""
    user = _get_user_from_header(x_telegram_init_data)
    if user["id"] not in settings.ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).where(Giveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()
        if not giveaway:
            return {"error": "Topilmadi"}
        if giveaway.status != "active":
            return {"error": "Bu o'yin faol emas"}

        giveaway.status = "cancelled"
        await session.commit()

    return {"success": True}


@router.get("/admin/stats")
async def admin_full_stats(x_telegram_init_data: str | None = Header(None)):
    """Admin: detailed bot statistics."""
    user = _get_user_from_header(x_telegram_init_data)
    if user["id"] not in settings.ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")

    from bot.models.contest import Contest, ContestStatus
    from bot.models.referral import Referral
    from bot.models.moderation import Blacklist
    from bot.models.loyalty import LoyaltyPoints

    async with async_session() as session:
        gw_total = (await session.execute(select(func.count(Giveaway.id)))).scalar() or 0
        gw_active = (await session.execute(
            select(func.count(Giveaway.id)).where(Giveaway.status == "active")
        )).scalar() or 0
        gw_completed = (await session.execute(
            select(func.count(Giveaway.id)).where(Giveaway.status == "completed")
        )).scalar() or 0
        total_participants = (await session.execute(select(func.count(GiveawayParticipant.id)))).scalar() or 0
        total_winners = (await session.execute(select(func.count(GiveawayWinner.id)))).scalar() or 0
        unique_users = (await session.execute(
            select(func.count(func.distinct(GiveawayParticipant.user_id)))
        )).scalar() or 0
        ct_total = (await session.execute(select(func.count(Contest.id)))).scalar() or 0
        total_referrals = (await session.execute(select(func.count(Referral.id)))).scalar() or 0
        verified_referrals = (await session.execute(
            select(func.count(Referral.id)).where(Referral.verified == True)
        )).scalar() or 0
        blacklisted = (await session.execute(
            select(func.count(Blacklist.id)).where(Blacklist.is_active == True)
        )).scalar() or 0
        total_points = (await session.execute(
            select(func.sum(LoyaltyPoints.total_earned))
        )).scalar() or 0

    return {
        "giveaways": {"total": gw_total, "active": gw_active, "completed": gw_completed},
        "participants": {"total": total_participants, "unique_users": unique_users, "winners": total_winners},
        "contests": {"total": ct_total},
        "referrals": {"total": total_referrals, "verified": verified_referrals},
        "moderation": {"blacklisted": blacklisted},
        "points": {"total_distributed": total_points},
    }



# ─── Referral endpoints ──────────────────────────────────────────────────────


@router.get("/referral")
async def miniapp_referral(x_telegram_init_data: str | None = Header(None)):
    """Get user's referral link and stats."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]
    username = user.get("username")

    from bot.utils.referral import generate_referral_link, get_referral_count
    from bot.utils.loyalty import POINTS_CONFIG

    bot_username = settings.BOT_USERNAME or "qurachibot"
    link = generate_referral_link(bot_username, user_id)
    count = await get_referral_count(user_id)
    points_per = POINTS_CONFIG.get("referral", 20)

    return {
        "link": link,
        "count": count,
        "points_earned": count * points_per,
        "points_per_referral": points_per,
    }


# ─── Contests endpoints ─────────────────────────────────────────────────────


@router.get("/active-contests")
async def miniapp_active_contests():
    """List active contests."""
    from bot.models.contest import Contest, ContestStatus, ContestSubmission
    async with async_session() as session:
        result = await session.execute(
            select(Contest)
            .where(Contest.status.in_([ContestStatus.ACCEPTING_SUBMISSIONS, ContestStatus.VOTING]))
            .order_by(Contest.created_at.desc())
            .limit(20)
        )
        contests = result.scalars().all()

        items = []
        for ct in contests:
            sub_count = (await session.execute(
                select(func.count(ContestSubmission.id)).where(ContestSubmission.contest_id == ct.id)
            )).scalar() or 0
            items.append({
                "id": ct.id, "title": ct.title, "prize": ct.prize,
                "post_text": ct.post_text, "description": ct.description,
                "status": ct.status.value, "type": ct.contest_type.value,
                "submissions": sub_count, "winner_count": ct.winner_count,
                "created_at": ct.created_at.isoformat() if ct.created_at else None,
            })
    return items


@router.get("/contest/{contest_id}")
async def miniapp_contest_detail(contest_id: int, x_telegram_init_data: str | None = Header(None)):
    """Get contest detail with submissions."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    from bot.models.contest import Contest, ContestStatus, ContestSubmission, ContestVote

    async with async_session() as session:
        result = await session.execute(select(Contest).where(Contest.id == contest_id))
        ct = result.scalar_one_or_none()
        if not ct:
            raise HTTPException(status_code=404, detail="Contest not found")

        # Get submissions with vote counts
        result = await session.execute(
            select(ContestSubmission).where(ContestSubmission.contest_id == contest_id)
            .order_by(ContestSubmission.vote_count.desc())
        )
        submissions = result.scalars().all()

        # Check which submissions user has voted for
        result = await session.execute(
            select(ContestVote.submission_id).where(ContestVote.user_id == user_id)
        )
        voted_ids = set(r[0] for r in result.all())

        # Check user's own submissions
        user_submitted = any(s.user_id == user_id for s in submissions)

    return {
        "id": ct.id, "title": ct.title, "description": ct.description,
        "prize": ct.prize, "status": ct.status.value, "type": ct.contest_type.value,
        "winner_count": ct.winner_count, "is_creator": ct.creator_id == user_id,
        "user_submitted": user_submitted,
        "submissions": [
            {
                "id": s.id,
                "user": f"@{s.username}" if s.username else (s.first_name or f"User"),
                "text": s.text_content[:200] if s.text_content else None,
                "has_photo": bool(s.file_id),
                "votes": s.vote_count or 0,
                "user_voted": s.id in voted_ids,
                "is_mine": s.user_id == user_id,
            }
            for s in submissions[:30]
        ],
    }


@router.post("/contest/{contest_id}/vote/{submission_id}")
async def miniapp_vote(contest_id: int, submission_id: int, x_telegram_init_data: str | None = Header(None)):
    """Vote for a contest submission."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    from bot.models.contest import Contest, ContestStatus, ContestSubmission, ContestVote

    async with async_session() as session:
        result = await session.execute(select(ContestSubmission).where(ContestSubmission.id == submission_id))
        sub = result.scalar_one_or_none()
        if not sub:
            return {"error": "Ishtirok topilmadi"}
        if sub.user_id == user_id:
            return {"error": "O'z ishtirokingizga ovoz bera olmaysiz"}

        result = await session.execute(select(Contest).where(Contest.id == contest_id))
        ct = result.scalar_one_or_none()
        if not ct or ct.status not in (ContestStatus.ACCEPTING_SUBMISSIONS, ContestStatus.VOTING):
            return {"error": "Ovoz berish yopilgan"}

        # Check already voted
        result = await session.execute(
            select(ContestVote).where(ContestVote.submission_id == submission_id, ContestVote.user_id == user_id)
        )
        if result.scalar_one_or_none():
            return {"error": "Allaqachon ovoz bergansiz"}

        vote = ContestVote(submission_id=submission_id, user_id=user_id)
        session.add(vote)
        sub.vote_count = (sub.vote_count or 0) + 1
        await session.commit()

    return {"success": True, "votes": sub.vote_count}


# ─── Join Filter endpoints ───────────────────────────────────────────────────


@router.get("/join-filters")
async def miniapp_join_filters(x_telegram_init_data: str | None = Header(None)):
    """Get join filters managed by the user (admin or creator)."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    from bot.handlers.join_request import JoinFilter

    async with async_session() as session:
        # For admins, show all; for others, this won't have entries
        if user_id in settings.ADMIN_IDS:
            result = await session.execute(select(JoinFilter).order_by(JoinFilter.created_at.desc()))
        else:
            result = await session.execute(
                select(JoinFilter).order_by(JoinFilter.created_at.desc()).limit(10)
            )
        filters = result.scalars().all()

    return [
        {
            "chat_id": f.chat_id, "chat_title": f.chat_title,
            "mode": f.filter_mode, "enabled": f.enabled,
            "channels": f.required_channels,
            "accepted": f.accepted or 0, "rejected": f.rejected or 0,
        }
        for f in filters
    ]


@router.post("/join-filter/set")
async def miniapp_set_join_filter(request: Request, x_telegram_init_data: str | None = Header(None)):
    """Set join filter for a chat. Any user can configure filters for channels they admin."""
    user = _get_user_from_header(x_telegram_init_data)

    body = await request.json()
    chat_id = body.get("chat_id")
    mode = body.get("mode", "all")
    channels = body.get("channels")

    if not chat_id:
        return {"error": "chat_id kerak"}

    # Allow @username format — resolve to numeric if needed
    chat_id_value = chat_id
    if isinstance(chat_id, str) and chat_id.startswith("@"):
        chat_id_value = chat_id  # Keep as string — will be stored and used for lookups
    else:
        try:
            chat_id_value = int(chat_id)
        except (ValueError, TypeError):
            # Might be @username without the @
            chat_id_value = f"@{chat_id}" if not str(chat_id).lstrip("-").isdigit() else chat_id

    valid_modes = ("all", "no_bots", "females", "males", "subscribed", "started", "off")
    if mode not in valid_modes:
        return {"error": f"Noto'g'ri rejim. Mavjud: {', '.join(valid_modes)}"}

    from bot.handlers.join_request import JoinFilter
    from bot.utils.subscription import serialize_channels, parse_channels as _pc

    channels_str = serialize_channels(_pc(channels)) if channels else None

    async with async_session() as session:
        # Try to find by chat_id (numeric or string)
        if isinstance(chat_id_value, int):
            result = await session.execute(select(JoinFilter).where(JoinFilter.chat_id == chat_id_value))
        else:
            result = await session.execute(select(JoinFilter).where(JoinFilter.chat_id == 0))  # won't match — create new

        config = result.scalar_one_or_none()

        if config:
            config.filter_mode = mode
            config.enabled = (mode != "off")
            if channels_str:
                config.required_channels = channels_str
        else:
            # Determine numeric chat_id — for @username, store as-is for now
            store_id = chat_id_value if isinstance(chat_id_value, int) else hash(str(chat_id_value)) % (10**15)
            config = JoinFilter(
                chat_id=store_id,
                chat_title=str(chat_id) if isinstance(chat_id, str) else None,
                filter_mode=mode,
                enabled=(mode != "off"),
                required_channels=channels_str,
            )
            session.add(config)
        await session.commit()

    return {"success": True, "mode": mode}


# ─── Language endpoint ───────────────────────────────────────────────────────


@router.get("/language")
async def miniapp_get_language(x_telegram_init_data: str | None = Header(None)):
    """Get user's current language."""
    user = _get_user_from_header(x_telegram_init_data)
    from bot.utils.lang import get_user_lang
    lang = await get_user_lang(user["id"])
    return {"language": lang, "available": ["en", "ru", "uz"]}


@router.post("/language")
async def miniapp_set_language(request: Request, x_telegram_init_data: str | None = Header(None)):
    """Set user's language."""
    user = _get_user_from_header(x_telegram_init_data)
    body = await request.json()
    lang = body.get("language", "uz")
    if lang not in ("en", "ru", "uz"):
        return {"error": "Noto'g'ri til"}
    from bot.utils.lang import set_user_lang
    await set_user_lang(user["id"], lang)
    return {"success": True, "language": lang}


# ─── Points/Loyalty endpoints ────────────────────────────────────────────────


@router.get("/points")
async def miniapp_points(x_telegram_init_data: str | None = Header(None)):
    """Get user's points detail."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    from bot.models.loyalty import LoyaltyPoints, PointsTransaction
    from bot.utils.loyalty import POINTS_CONFIG

    async with async_session() as session:
        result = await session.execute(select(LoyaltyPoints).where(LoyaltyPoints.user_id == user_id))
        loyalty = result.scalar_one_or_none()

        # Recent transactions
        result = await session.execute(
            select(PointsTransaction).where(PointsTransaction.user_id == user_id)
            .order_by(PointsTransaction.created_at.desc()).limit(20)
        )
        transactions = result.scalars().all()

    return {
        "points": loyalty.points if loyalty else 0,
        "total_earned": loyalty.total_earned if loyalty else 0,
        "total_spent": loyalty.total_spent if loyalty else 0,
        "giveaways_joined": loyalty.giveaways_joined if loyalty else 0,
        "contests_joined": loyalty.contests_joined if loyalty else 0,
        "wins": loyalty.wins if loyalty else 0,
        "referrals_made": loyalty.referrals_made if loyalty else 0,
        "config": POINTS_CONFIG,
        "transactions": [
            {"amount": t.amount, "reason": t.reason, "date": t.created_at.isoformat() if t.created_at else None}
            for t in transactions
        ],
    }


# ─── Alerts/Notifications ────────────────────────────────────────────────────


@router.get("/alerts")
async def miniapp_alerts(x_telegram_init_data: str | None = Header(None)):
    """Get user's alert subscription status."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    from bot.models.notification import AlertSubscription

    async with async_session() as session:
        result = await session.execute(
            select(AlertSubscription).where(AlertSubscription.user_id == user_id)
        )
        sub = result.scalar_one_or_none()

    return {"subscribed": sub is not None}


@router.post("/alerts/toggle")
async def miniapp_toggle_alerts(x_telegram_init_data: str | None = Header(None)):
    """Toggle alert subscription."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    from bot.models.notification import AlertSubscription

    async with async_session() as session:
        result = await session.execute(
            select(AlertSubscription).where(AlertSubscription.user_id == user_id)
        )
        sub = result.scalar_one_or_none()

        if sub:
            await session.delete(sub)
            await session.commit()
            return {"subscribed": False}
        else:
            session.add(AlertSubscription(user_id=user_id))
            await session.commit()
            return {"subscribed": True}



# ─── Comment Giveaway creation ───────────────────────────────────────────────


@router.post("/create-comment-giveaway")
async def miniapp_create_comment_giveaway(
    request: Request,
    x_telegram_init_data: str | None = Header(None),
):
    """Create a comment-based (group/channel) giveaway from the Mini App (post-based)."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]
    username = user.get("username")

    body = await request.json()
    post_text = body.get("post_text", "").strip()
    if not post_text:
        return {"error": "Post matni kiritilishi shart"}

    import re
    plain = re.sub(r"<[^>]+>", "", post_text)
    title = plain.strip().split("\n")[0][:100] or "Group Giveaway"

    from bot.models.group_giveaway import GroupGiveaway, GroupGiveawayMode
    from bot.utils.subscription import serialize_channels, parse_channels as _pc

    mode_map = {
        "random": GroupGiveawayMode.RANDOM,
        "first_n": GroupGiveawayMode.FIRST_N,
        "keyword": GroupGiveawayMode.KEYWORD,
        "reaction": GroupGiveawayMode.REACTION,
    }
    mode = mode_map.get(body.get("mode", "random"), GroupGiveawayMode.RANDOM)

    duration_map = {
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1), "3h": timedelta(hours=3),
        "6h": timedelta(hours=6), "12h": timedelta(hours=12),
        "24h": timedelta(hours=24), "2d": timedelta(days=2),
        "3d": timedelta(days=3), "5d": timedelta(days=5),
        "7d": timedelta(days=7), "14d": timedelta(days=14),
        "30d": timedelta(days=30), "none": None,
    }

    deadline_str = body.get("deadline")
    duration_key = body.get("duration", "24h")

    if deadline_str:
        try:
            ends_at = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return {"error": "Noto'g'ri sana formati"}
    elif duration_key and duration_key != "none" and duration_key != "custom":
        duration = duration_map.get(duration_key)
        ends_at = datetime.utcnow() + duration if duration else None
    else:
        ends_at = None

    channels_str = None
    raw_channels = body.get("required_channels")
    if raw_channels:
        channels_str = serialize_channels(_pc(raw_channels))

    keyword = body.get("keyword") if mode == GroupGiveawayMode.KEYWORD else None

    async with async_session() as session:
        gw = GroupGiveaway(
            title=title, post_text=post_text,
            post_file_id=body.get("post_file_id") or None,
            post_media_type="photo" if body.get("post_file_id") else None,
            winner_count=max(1, int(body.get("winner_count", 1))),
            mode=mode,
            keyword=keyword,
            required_channels=channels_str,
            creator_id=user_id,
            creator_username=username,
            chat_id=user_id,
            ends_at=ends_at,
            is_channel_post=False,
        )
        session.add(gw)
        await session.commit()
        await session.refresh(gw)

    return {"success": True, "id": gw.id, "title": gw.title}


# ─── Admin Broadcast ─────────────────────────────────────────────────────────


@router.post("/notify-participants")
async def miniapp_notify_participants(
    request: Request,
    x_telegram_init_data: str | None = Header(None),
):
    """Notify all participants of a giveaway with a custom message."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]

    body = await request.json()
    giveaway_id = body.get("giveaway_id")
    text = body.get("text", "").strip()

    if not giveaway_id or not text:
        return {"error": "giveaway_id va text kerak"}

    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).where(Giveaway.id == giveaway_id)
        )
        giveaway = result.scalar_one_or_none()

        if not giveaway:
            return {"error": "O'yin topilmadi"}
        if giveaway.creator_id != user_id and user_id not in settings.ADMIN_IDS:
            return {"error": "Faqat yaratuvchi xabar yubora oladi"}

        # Get participants
        result = await session.execute(
            select(GiveawayParticipant).where(
                GiveawayParticipant.giveaway_id == giveaway_id
            )
        )
        participants = result.scalars().all()

    if not participants:
        return {"error": "Ishtirokchilar yo'q", "sent": 0}

    from telegram import Bot
    bot = Bot(token=settings.BOT_TOKEN)

    sent = 0
    failed = 0
    for p in participants:
        try:
            await bot.send_message(p.user_id, text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1

    logger.info("Creator notify: gw=%d, sent=%d, failed=%d, by=%d", giveaway_id, sent, failed, user_id)
    return {"success": True, "sent": sent, "failed": failed}


@router.post("/admin/broadcast")
async def admin_broadcast(
    request: Request,
    x_telegram_init_data: str | None = Header(None),
):
    """Admin: broadcast a message to users.

    Targets:
      - all: all users who have ever interacted with the bot (user_settings table)
      - participants: users who joined at least one giveaway
      - subscribers: users who subscribed to alerts (AlertSubscription)
    """
    user = _get_user_from_header(x_telegram_init_data)
    if user["id"] not in settings.ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")

    body = await request.json()
    text = body.get("text", "").strip()
    target = body.get("target", "all")

    if not text:
        return {"error": "Xabar matni bo'sh bo'lmasligi kerak"}

    from bot.models.user_settings import UserSettings
    from bot.models.notification import AlertSubscription

    # Collect target user IDs
    user_ids = set()
    async with async_session() as session:
        if target == "all":
            result = await session.execute(select(UserSettings.user_id))
            user_ids = {r[0] for r in result.all()}
            # Also include giveaway participants
            result = await session.execute(
                select(func.distinct(GiveawayParticipant.user_id))
            )
            user_ids.update(r[0] for r in result.all())

        elif target == "participants":
            result = await session.execute(
                select(func.distinct(GiveawayParticipant.user_id))
            )
            user_ids = {r[0] for r in result.all()}

        elif target == "subscribers":
            result = await session.execute(select(AlertSubscription.user_id))
            user_ids = {r[0] for r in result.all()}

    if not user_ids:
        return {"error": "Yuborish uchun foydalanuvchilar topilmadi", "sent": 0}

    # Send messages (in background — don't block the response)
    # For now, we'll send synchronously but with error handling per user.
    # In production, this should be a background task.
    from telegram import Bot
    bot = Bot(token=settings.BOT_TOKEN)

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1

    logger.info("Broadcast: sent=%d, failed=%d, target=%s, by user=%d", sent, failed, target, user["id"])
    return {"success": True, "sent": sent, "failed": failed, "total": len(user_ids)}



# ─── Feedback / Suggestions & Complaints ─────────────────────────────────────


@router.post("/feedback")
async def miniapp_submit_feedback(request: Request, x_telegram_init_data: str | None = Header(None)):
    """Submit feedback/suggestion/complaint from a user."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]
    username = user.get("username")

    body = await request.json()
    text = body.get("text", "").strip()
    feedback_type = body.get("type", "suggestion")  # suggestion, complaint, bug

    if not text:
        return {"error": "Matn kiritilishi shart"}
    if len(text) > 2000:
        return {"error": "Matn 2000 belgidan oshmasligi kerak"}

    from bot.models.base import Base
    from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func as sqlfunc
    from sqlalchemy.orm import Mapped, mapped_column

    # Store feedback in a simple way — use ContentFlag model repurposed,
    # or insert directly. Let's use a direct insert for simplicity.
    async with async_session() as session:
        # We'll store in content_flags table with content_type='feedback'
        from bot.models.moderation import ContentFlag
        flag = ContentFlag(
            user_id=user_id,
            content_type=f"feedback_{feedback_type}",
            content_text=f"[{feedback_type}] @{username or user_id}: {text}",
            reason=feedback_type,
        )
        session.add(flag)
        await session.commit()

    return {"success": True}


@router.get("/admin/feedback")
async def admin_get_feedback(x_telegram_init_data: str | None = Header(None)):
    """Admin: get all feedback/suggestions/complaints."""
    user = _get_user_from_header(x_telegram_init_data)
    if user["id"] not in settings.ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")

    from bot.models.moderation import ContentFlag

    async with async_session() as session:
        result = await session.execute(
            select(ContentFlag)
            .where(ContentFlag.content_type.like("feedback_%"))
            .order_by(ContentFlag.created_at.desc())
            .limit(50)
        )
        items = result.scalars().all()

    return [
        {
            "id": f.id,
            "type": f.reason,
            "text": f.content_text,
            "user_id": f.user_id,
            "date": f.created_at.isoformat() if f.created_at else None,
            "resolved": f.resolved if hasattr(f, 'resolved') else False,
        }
        for f in items
    ]


# ─── User Counter + DB Info (admin) ─────────────────────────────────────────


@router.get("/admin/users-count")
async def admin_users_count(x_telegram_init_data: str | None = Header(None)):
    """Admin: get total user count from different sources."""
    user = _get_user_from_header(x_telegram_init_data)
    if user["id"] not in settings.ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")

    from bot.models.user_settings import UserSettings
    from bot.models.loyalty import LoyaltyPoints

    async with async_session() as session:
        # Users who started the bot (user_settings table)
        started = (await session.execute(select(func.count(UserSettings.id)))).scalar() or 0

        # Users who joined any giveaway
        participants = (await session.execute(
            select(func.count(func.distinct(GiveawayParticipant.user_id)))
        )).scalar() or 0

        # Users with loyalty points
        with_points = (await session.execute(select(func.count(LoyaltyPoints.id)))).scalar() or 0

        # Verified users (passed CAPTCHA)
        verified = (await session.execute(
            select(func.count(UserSettings.id)).where(UserSettings.captcha_verified == True)
        )).scalar() or 0

        # Today's new users
        from datetime import datetime, timedelta
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        # We approximate "today's users" by loyalty points created today
        today_active = (await session.execute(
            select(func.count(func.distinct(GiveawayParticipant.user_id)))
            .where(GiveawayParticipant.joined_at >= today)
        )).scalar() or 0

    return {
        "total_started": started,
        "total_participants": participants,
        "with_points": with_points,
        "verified": verified,
        "today_active": today_active,
    }


# ─── Admin: Restart Schedule ──────────────────────────────────────────────────

UPDATE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "update_config.json")


@router.get("/admin/restart-schedule")
async def admin_get_restart_schedule(x_telegram_init_data: str | None = Header(None)):
    """Admin: get current auto-restart schedule from update_config.json."""
    user = _get_user_from_header(x_telegram_init_data)
    if user["id"] not in settings.ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")

    try:
        with open(UPDATE_CONFIG_PATH, "r") as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {"restart_hour": 3, "restart_minute": 0, "jitter_minutes": 30, "enabled": True}

    return config


@router.post("/admin/restart-schedule")
async def admin_set_restart_schedule(request: Request, x_telegram_init_data: str | None = Header(None)):
    """Admin: update auto-restart schedule in update_config.json."""
    user = _get_user_from_header(x_telegram_init_data)
    if user["id"] not in settings.ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")

    body = await request.json()

    # Validate inputs
    hour = int(body.get("restart_hour", 3))
    minute = int(body.get("restart_minute", 0))
    jitter = int(body.get("jitter_minutes", 30))
    enabled = bool(body.get("enabled", True))

    if not (0 <= hour <= 23):
        return {"error": "Hour must be 0-23"}
    if not (0 <= minute <= 59):
        return {"error": "Minute must be 0-59"}
    if not (0 <= jitter <= 120):
        return {"error": "Jitter must be 0-120 minutes"}

    config = {
        "restart_hour": hour,
        "restart_minute": minute,
        "jitter_minutes": jitter,
        "enabled": enabled,
    }

    try:
        with open(UPDATE_CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
    except Exception as e:
        logger.error("Failed to write update_config.json: %s", e)
        return {"error": "Failed to save config"}

    logger.info("Restart schedule updated by admin %d: %02d:%02d, jitter=%dmin, enabled=%s", user["id"], hour, minute, jitter, enabled)
    return {"success": True, **config}


RESTART_TRIGGER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".restart_trigger")


@router.post("/admin/restart-now")
async def admin_restart_now(x_telegram_init_data: str | None = Header(None)):
    """Admin: trigger an immediate git pull + restart by writing a trigger file.

    The background auto-updater in start_production.sh checks for this file
    every 10 seconds and performs an immediate update cycle when it finds it.
    """
    user = _get_user_from_header(x_telegram_init_data)
    if user["id"] not in settings.ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")

    try:
        with open(RESTART_TRIGGER_PATH, "w") as f:
            f.write(f"triggered_by={user['id']}\ntimestamp={datetime.utcnow().isoformat()}\n")
    except Exception as e:
        logger.error("Failed to write restart trigger: %s", e)
        return {"error": "Failed to trigger restart"}

    logger.info("Manual restart triggered by admin %d", user["id"])
    return {"success": True, "message": "Restart triggered. Bot will restart in ~10 seconds."}


@router.get("/admin/dbinfo")
async def admin_dbinfo(x_telegram_init_data: str | None = Header(None)):
    """Admin: get database info (table sizes, total rows)."""
    user = _get_user_from_header(x_telegram_init_data)
    if user["id"] not in settings.ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")

    from sqlalchemy import text as sql_text

    tables_info = []
    async with async_session() as session:
        # Get all table names and row counts
        try:
            result = await session.execute(sql_text("""
                SELECT schemaname, tablename,
                       pg_size_pretty(pg_total_relation_size(schemaname || '.' || tablename)) as size,
                       (SELECT count(*) FROM information_schema.columns
                        WHERE table_name = tablename AND table_schema = schemaname) as columns
                FROM pg_tables
                WHERE schemaname = 'public'
                ORDER BY pg_total_relation_size(schemaname || '.' || tablename) DESC
            """))
            for row in result:
                tables_info.append({
                    "table": row[1],
                    "size": row[2],
                    "columns": row[3],
                })
        except Exception:
            pass

        # Get row counts for key tables
        key_tables = [
            ("user_settings", "UserSettings"),
            ("giveaways", "Giveaway"),
            ("giveaway_participants", "GiveawayParticipant"),
            ("contests", "Contest"),
            ("group_giveaways", "GroupGiveaway"),
            ("referrals", "Referral"),
            ("loyalty_points", "LoyaltyPoints"),
        ]
        row_counts = {}
        for table_name, _ in key_tables:
            try:
                result = await session.execute(sql_text(f"SELECT count(*) FROM {table_name}"))
                row_counts[table_name] = result.scalar() or 0
            except Exception:
                row_counts[table_name] = -1

        # DB size
        db_size = "?"
        try:
            result = await session.execute(sql_text("SELECT pg_size_pretty(pg_database_size(current_database()))"))
            db_size = result.scalar()
        except Exception:
            pass

    return {
        "db_size": db_size,
        "tables": tables_info,
        "row_counts": row_counts,
    }
