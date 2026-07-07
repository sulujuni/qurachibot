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
        if giveaway.status != GiveawayStatus.ACTIVE:
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

        giveaway.status = GiveawayStatus.COMPLETED
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
        if giveaway.status != GiveawayStatus.ACTIVE:
            return {"error": "Bu o'yin faol emas"}

        giveaway.status = GiveawayStatus.CANCELLED
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
            select(func.count(Giveaway.id)).where(Giveaway.status == GiveawayStatus.ACTIVE)
        )).scalar() or 0
        gw_completed = (await session.execute(
            select(func.count(Giveaway.id)).where(Giveaway.status == GiveawayStatus.COMPLETED)
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
    """Set join filter for a chat (admin only)."""
    user = _get_user_from_header(x_telegram_init_data)
    if user["id"] not in settings.ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")

    body = await request.json()
    chat_id = body.get("chat_id")
    mode = body.get("mode", "all")
    channels = body.get("channels")

    if not chat_id:
        return {"error": "chat_id kerak"}

    valid_modes = ("all", "no_bots", "females", "males", "subscribed", "started", "off")
    if mode not in valid_modes:
        return {"error": f"Noto'g'ri rejim. Mavjud: {', '.join(valid_modes)}"}

    from bot.handlers.join_request import JoinFilter
    from bot.utils.subscription import serialize_channels, parse_channels as _pc

    channels_str = serialize_channels(_pc(channels)) if channels else None

    async with async_session() as session:
        result = await session.execute(select(JoinFilter).where(JoinFilter.chat_id == int(chat_id)))
        config = result.scalar_one_or_none()

        if config:
            config.filter_mode = mode
            config.enabled = (mode != "off")
            if channels_str:
                config.required_channels = channels_str
        else:
            config = JoinFilter(
                chat_id=int(chat_id), filter_mode=mode,
                enabled=(mode != "off"), required_channels=channels_str,
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
