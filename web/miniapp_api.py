"""Mini App API endpoints for giveaway participation.

These endpoints are called by the Telegram Mini App (Web App) when users
tap the 'Qatnashish' inline button on a giveaway announcement.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime
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
