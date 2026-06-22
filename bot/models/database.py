"""Database engine and session configuration — PostgreSQL (asyncpg)."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bot.config import settings

# Configure engine based on database driver
_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

if _is_sqlite:
    # SQLite fallback for local development
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
else:
    # PostgreSQL with connection pooling for production
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,  # Verify connections before use
        pool_recycle=3600,   # Recycle connections every hour
    )

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Create all tables if they don't exist."""
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


async def close_db():
    """Close the database engine (call on shutdown)."""
    await engine.dispose()
