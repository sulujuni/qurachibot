"""Referral system utilities."""

import base64
import struct

from sqlalchemy import select, func

from bot.models.database import async_session
from bot.models.referral import Referral


def generate_referral_link(bot_username: str, user_id: int, giveaway_id: int = None) -> str:
    """Generate a referral deep link for a user."""
    # Encode: ref_<user_id>_<giveaway_id>
    if giveaway_id:
        payload = f"ref_{user_id}_{giveaway_id}"
    else:
        payload = f"ref_{user_id}"
    return f"https://t.me/{bot_username}?start={payload}"


def parse_referral_payload(payload: str) -> tuple[int, int]:
    """Parse a referral start payload. Returns (referrer_id, giveaway_id or 0)."""
    if not payload or not payload.startswith("ref_"):
        return 0, 0

    parts = payload.split("_")
    try:
        referrer_id = int(parts[1])
        giveaway_id = int(parts[2]) if len(parts) > 2 else 0
        return referrer_id, giveaway_id
    except (IndexError, ValueError):
        return 0, 0


async def record_referral(
    referrer_id: int,
    referred_id: int,
    giveaway_id: int = None,
    referrer_username: str = None,
    referred_username: str = None,
) -> bool:
    """Record a referral. Returns True if new, False if already referred."""
    if referrer_id == referred_id:
        return False

    async with async_session() as session:
        # Check if this user was already referred
        result = await session.execute(
            select(Referral).where(Referral.referred_id == referred_id)
        )
        if result.scalar_one_or_none():
            return False  # Already referred by someone

        referral = Referral(
            referrer_id=referrer_id,
            referred_id=referred_id,
            giveaway_id=giveaway_id,
            referrer_username=referrer_username,
            referred_username=referred_username,
        )
        session.add(referral)
        await session.commit()

    return True


async def get_referral_count(user_id: int) -> int:
    """Get how many users this person has referred."""
    async with async_session() as session:
        result = await session.execute(
            select(func.count(Referral.id)).where(Referral.referrer_id == user_id)
        )
        return result.scalar()


async def get_bonus_entries(user_id: int, giveaway_id: int) -> int:
    """Get total bonus entries a user has for a giveaway through referrals."""
    async with async_session() as session:
        result = await session.execute(
            select(func.sum(Referral.bonus_entries)).where(
                Referral.referrer_id == user_id,
                Referral.giveaway_id == giveaway_id,
            )
        )
        total = result.scalar()
        return total if total else 0
