"""Loyalty points system utility."""

from sqlalchemy import select

from bot.models.database import async_session
from bot.models.loyalty import LoyaltyPoints, PointsTransaction

# Points configuration
POINTS_CONFIG = {
    "join_giveaway": 5,
    "join_contest": 5,
    "submit_entry": 10,
    "vote": 2,
    "win_giveaway": 50,
    "win_contest": 75,
    "referral": 20,
    "daily_bonus": 3,
    "extra_entry_cost": 50,  # Cost to redeem an extra entry
}


async def get_or_create_loyalty(user_id: int, username: str = None, first_name: str = None) -> LoyaltyPoints:
    """Get or create a loyalty record for a user."""
    async with async_session() as session:
        result = await session.execute(
            select(LoyaltyPoints).where(LoyaltyPoints.user_id == user_id)
        )
        loyalty = result.scalar_one_or_none()

        if not loyalty:
            loyalty = LoyaltyPoints(
                user_id=user_id,
                username=username,
                first_name=first_name,
                points=0,
                total_earned=0,
                total_spent=0,
            )
            session.add(loyalty)
            await session.commit()
            await session.refresh(loyalty)

        return loyalty


async def award_points(user_id: int, reason: str, description: str = None, username: str = None, first_name: str = None) -> int:
    """Award points to a user. Returns the amount awarded."""
    amount = POINTS_CONFIG.get(reason, 0)
    if amount <= 0:
        return 0

    async with async_session() as session:
        result = await session.execute(
            select(LoyaltyPoints).where(LoyaltyPoints.user_id == user_id)
        )
        loyalty = result.scalar_one_or_none()

        if not loyalty:
            loyalty = LoyaltyPoints(
                user_id=user_id,
                username=username,
                first_name=first_name,
                points=amount,
                total_earned=amount,
                total_spent=0,
                giveaways_joined=0,
                contests_joined=0,
                wins=0,
                referrals_made=0,
            )
            session.add(loyalty)
        else:
            loyalty.points += amount
            loyalty.total_earned += amount
            if username:
                loyalty.username = username
            if first_name:
                loyalty.first_name = first_name

        # Track stat (defensive: counters may be unset on a brand-new record)
        if reason == "join_giveaway":
            loyalty.giveaways_joined = (loyalty.giveaways_joined or 0) + 1
        elif reason in ("join_contest", "submit_entry"):
            loyalty.contests_joined = (loyalty.contests_joined or 0) + 1
        elif reason in ("win_giveaway", "win_contest"):
            loyalty.wins = (loyalty.wins or 0) + 1
        elif reason == "referral":
            loyalty.referrals_made = (loyalty.referrals_made or 0) + 1

        # Log transaction
        tx = PointsTransaction(
            user_id=user_id,
            amount=amount,
            reason=reason,
            description=description,
        )
        session.add(tx)
        await session.commit()

    return amount


async def spend_points(user_id: int, amount: int, reason: str, description: str = None) -> bool:
    """Spend points. Returns True if successful, False if insufficient."""
    async with async_session() as session:
        result = await session.execute(
            select(LoyaltyPoints).where(LoyaltyPoints.user_id == user_id)
        )
        loyalty = result.scalar_one_or_none()

        if not loyalty or loyalty.points < amount:
            return False

        loyalty.points -= amount
        loyalty.total_spent += amount

        tx = PointsTransaction(
            user_id=user_id,
            amount=-amount,
            reason=reason,
            description=description,
        )
        session.add(tx)
        await session.commit()

    return True


async def get_leaderboard(limit: int = 10) -> list[LoyaltyPoints]:
    """Get top users by points."""
    async with async_session() as session:
        result = await session.execute(
            select(LoyaltyPoints)
            .order_by(LoyaltyPoints.total_earned.desc())
            .limit(limit)
        )
        return result.scalars().all()


async def get_user_rank(user_id: int) -> int:
    """Get a user's rank on the leaderboard."""
    async with async_session() as session:
        result = await session.execute(
            select(LoyaltyPoints).where(LoyaltyPoints.user_id == user_id)
        )
        user_loyalty = result.scalar_one_or_none()
        if not user_loyalty:
            return 0

        result = await session.execute(
            select(LoyaltyPoints).where(
                LoyaltyPoints.total_earned > user_loyalty.total_earned
            )
        )
        higher_count = len(result.scalars().all())
        return higher_count + 1
