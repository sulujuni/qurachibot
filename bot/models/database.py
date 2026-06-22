"""Database engine and session configuration."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Create all tables."""
    from bot.models.base import Base  # noqa: F401
    import bot.models.giveaway  # noqa: F401
    import bot.models.contest  # noqa: F401
    import bot.models.user_settings  # noqa: F401
    import bot.models.referral  # noqa: F401
    import bot.models.loyalty  # noqa: F401
    import bot.models.moderation  # noqa: F401
    import bot.models.notification  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
