"""Rate limiting utility."""

from datetime import datetime, timedelta

from sqlalchemy import select, func

from bot.models.database import async_session
from bot.models.moderation import RateLimitLog

# Rate limits: max actions per time window
RATE_LIMITS = {
    "join": (5, timedelta(minutes=1)),       # 5 joins per minute
    "submit": (3, timedelta(minutes=1)),     # 3 submissions per minute
    "vote": (10, timedelta(minutes=1)),      # 10 votes per minute
    "create": (2, timedelta(minutes=5)),     # 2 creates per 5 minutes
    "referral": (10, timedelta(hours=1)),    # 10 referrals per hour
}


async def check_rate_limit(user_id: int, action: str) -> bool:
    """Check if user is within rate limits. Returns True if allowed, False if limited."""
    if action not in RATE_LIMITS:
        return True

    max_actions, window = RATE_LIMITS[action]
    since = datetime.utcnow() - window

    async with async_session() as session:
        result = await session.execute(
            select(func.count(RateLimitLog.id)).where(
                RateLimitLog.user_id == user_id,
                RateLimitLog.action == action,
                RateLimitLog.timestamp >= since,
            )
        )
        count = result.scalar()

    return count < max_actions


async def log_action(user_id: int, action: str) -> None:
    """Log an action for rate limiting."""
    async with async_session() as session:
        log = RateLimitLog(user_id=user_id, action=action)
        session.add(log)
        await session.commit()
