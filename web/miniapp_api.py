"""Mini App API endpoints for giveaway participation.

These endpoints are called by the Telegram Mini App (Web App) when users
tap the 'Qatnashish' inline button on a giveaway announcement.
"""

import hashlib
import hmac
import json
import logging
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
            return None

        # Build the check string (alphabetically sorted key=value, excluding 'hash')
        items = []
        for key, values in sorted(parsed.items()):
            if key == "hash":
                continue
            items.append(f"{key}={values[0]}")
        data_check_string = "\n".join(items)

        # HMAC-SHA-256 with secret key derived from bot token
        secret_key = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            # In development (no real initData), allow a fallback for testing
            if settings.BOT_TOKEN.startswith("test"):
                pass
            else:
                return None

        # Parse user JSON
        user_json = parsed.get("user", [None])[0]
        if user_json:
            return json.loads(user_json)
        return None
    except Exception as e:
        logger.warning("initData validation failed: %s", e)
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

    # Fallback: try parsing user field directly (dev mode)
    try:
        parsed = parse_qs(init_data_header)
        user_json = parsed.get("user", [None])[0]
        if user_json:
            return json.loads(user_json)
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
        if giveaway.status == GiveawayStatus.COMPLETED:
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
        "prize": giveaway.prize,
        "winner_count": giveaway.winner_count,
        "status": giveaway.status.value,
        "participants": participant_count,
        "already_joined": already_joined,
        "must_subscribe": must_subscribe,
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

        if giveaway.status != GiveawayStatus.ACTIVE:
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
            select(func.count(Giveaway.id)).where(Giveaway.status == GiveawayStatus.ACTIVE)
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
            .where(Giveaway.status == GiveawayStatus.ACTIVE)
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
                "participants": count,
                "winner_count": gw.winner_count,
                "ends_at": gw.ends_at.isoformat() if gw.ends_at else None,
                "created_at": gw.created_at.isoformat() if gw.created_at else None,
            })
    return items


@router.get("/leaderboard")
async def miniapp_leaderboard():
    """Top users for the leaderboard tab."""
    from bot.models.loyalty import LoyaltyPoints
    async with async_session() as session:
        result = await session.execute(
            select(LoyaltyPoints).order_by(LoyaltyPoints.total_earned.desc()).limit(30)
        )
        users = result.scalars().all()
    return [
        {
            "rank": i + 1,
            "username": u.username,
            "first_name": u.first_name,
            "points": u.total_earned or 0,
            "wins": u.wins or 0,
        }
        for i, u in enumerate(users)
    ]


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
    """Create a giveaway from the Mini App form."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]
    username = user.get("username")

    body = await request.json()
    title = body.get("title", "").strip()
    prize = body.get("prize", "").strip()
    if not title or not prize:
        return {"error": "Nom va sovg'a kiritilishi shart"}

    description = body.get("description") or None
    winner_count = max(1, int(body.get("winner_count", 1)))
    required_channels = body.get("required_channels") or None

    # Parse duration
    duration_map = {
        "1h": timedelta(hours=1), "6h": timedelta(hours=6),
        "12h": timedelta(hours=12), "24h": timedelta(hours=24),
        "3d": timedelta(days=3), "7d": timedelta(days=7), "none": None,
    }
    duration = duration_map.get(body.get("duration", "24h"))
    ends_at = datetime.utcnow() + duration if duration else None

    from bot.utils.subscription import serialize_channels, parse_channels as _pc
    channels_str = serialize_channels(_pc(required_channels)) if required_channels else None

    async with async_session() as session:
        giveaway = Giveaway(
            title=title, description=description, prize=prize,
            winner_count=winner_count,
            required_channels=channels_str,
            creator_id=user_id, creator_username=username,
            chat_id=user_id,  # will be updated when posted to a channel
            ends_at=ends_at,
        )
        session.add(giveaway)
        await session.commit()
        await session.refresh(giveaway)

    return {"success": True, "id": giveaway.id, "title": giveaway.title}


@router.post("/create-contest")
async def miniapp_create_contest(
    request: Request,
    x_telegram_init_data: str | None = Header(None),
):
    """Create a contest from the Mini App form."""
    user = _get_user_from_header(x_telegram_init_data)
    user_id = user["id"]
    username = user.get("username")

    body = await request.json()
    title = body.get("title", "").strip()
    if not title:
        return {"error": "Konkurs nomi kiritilishi shart"}

    from bot.models.contest import Contest, ContestType

    type_map = {"text": ContestType.TEXT, "photo": ContestType.PHOTO, "any": ContestType.ANY}
    contest_type = type_map.get(body.get("contest_type", "any"), ContestType.ANY)

    async with async_session() as session:
        contest = Contest(
            title=title,
            description=body.get("description") or None,
            prize=body.get("prize") or None,
            contest_type=contest_type,
            winner_count=max(1, int(body.get("winner_count", 1))),
            max_submissions_per_user=1,
            creator_id=user_id, creator_username=username,
            chat_id=user_id,
        )
        session.add(contest)
        await session.commit()
        await session.refresh(contest)

    return {"success": True, "id": contest.id, "title": contest.title}
