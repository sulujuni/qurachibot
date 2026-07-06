"""Referral system utilities."""

import logging

from sqlalchemy import select, func

from bot.config import settings
from bot.models.database import async_session
from bot.models.giveaway import Giveaway
from bot.models.referral import Referral
from bot.utils.subscription import get_unsubscribed, parse_channels

logger = logging.getLogger(__name__)


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


async def _required_channels_for(giveaway_id: int | None) -> list[str]:
    """Resolve the channels a referred user must be subscribed to.

    Uses the giveaway's required channels when a giveaway_id is supplied,
    otherwise falls back to the bot-wide REFERRAL_REQUIRED_CHANNELS setting.
    """
    if giveaway_id:
        async with async_session() as session:
            result = await session.execute(
                select(Giveaway.required_channels).where(Giveaway.id == giveaway_id)
            )
            raw = result.scalar_one_or_none()
        channels = parse_channels(raw)
        if channels:
            return channels
    return parse_channels(",".join(settings.REFERRAL_REQUIRED_CHANNELS))


async def process_referral(
    bot,
    referrer_id: int,
    referred_user,
    giveaway_id: int | None = None,
) -> str:
    """Validate and record a referral from a /start deep link.

    Anti-abuse rules:
      • Bot accounts are never counted.
      • Self-referrals are ignored.
      • A user can only be referred once (by the first referrer).
      • The referral only becomes *verified* (and awards points) once the
        referred user is subscribed to all required channels. Until then it
        is stored as pending and can be verified later.

    Returns one of: "bot", "self", "already", "verified", "pending".
    """
    # Reject bot accounts outright — they must never count.
    if getattr(referred_user, "is_bot", False):
        return "bot"

    referred_id = referred_user.id
    if referrer_id == referred_id:
        return "self"

    required_channels = await _required_channels_for(giveaway_id)
    is_subscribed = True
    if required_channels:
        missing = await get_unsubscribed(bot, referred_id, required_channels)
        is_subscribed = not missing

    async with async_session() as session:
        result = await session.execute(
            select(Referral).where(Referral.referred_id == referred_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            # Upgrade a previously pending referral if the user is now subscribed.
            if not existing.verified and is_subscribed:
                existing.verified = True
                await session.commit()
                await _award_referrer(existing.referrer_id, existing.referrer_username)
                return "verified"
            return "already"

        referral = Referral(
            referrer_id=referrer_id,
            referred_id=referred_id,
            referred_username=getattr(referred_user, "username", None),
            giveaway_id=giveaway_id or None,
            verified=is_subscribed,
        )
        session.add(referral)
        await session.commit()

    if is_subscribed:
        await _award_referrer(referrer_id, None)
        return "verified"
    return "pending"


async def verify_pending_referrals(bot, referred_id: int) -> None:
    """Re-check a user's pending referral and verify it if now subscribed.

    Call this whenever a user proves channel membership (e.g. joins a
    giveaway) so referrals that were pending at /start time get credited.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Referral).where(
                Referral.referred_id == referred_id,
                Referral.verified == False,  # noqa: E712
            )
        )
        referral = result.scalar_one_or_none()

    if not referral:
        return

    required_channels = await _required_channels_for(referral.giveaway_id)
    if required_channels:
        missing = await get_unsubscribed(bot, referred_id, required_channels)
        if missing:
            return  # still not subscribed

    async with async_session() as session:
        result = await session.execute(
            select(Referral).where(Referral.id == referral.id)
        )
        row = result.scalar_one_or_none()
        if not row or row.verified:
            return
        row.verified = True
        await session.commit()

    await _award_referrer(referral.referrer_id, referral.referrer_username)


async def _award_referrer(referrer_id: int, referrer_username: str | None) -> None:
    """Award loyalty points to the referrer for a verified referral."""
    # Imported lazily to avoid a circular import at module load.
    from bot.utils.loyalty import award_points

    try:
        await award_points(referrer_id, "referral", username=referrer_username)
    except Exception as e:
        logger.warning("Failed to award referral points to %s: %s", referrer_id, e)


async def get_referral_count(user_id: int, verified_only: bool = True) -> int:
    """Get how many users this person has referred (verified by default)."""
    async with async_session() as session:
        stmt = select(func.count(Referral.id)).where(Referral.referrer_id == user_id)
        if verified_only:
            stmt = stmt.where(Referral.verified == True)  # noqa: E712
        result = await session.execute(stmt)
        return result.scalar() or 0


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
