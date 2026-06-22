"""Content moderation and blacklist utilities."""

import re

from sqlalchemy import select

from bot.models.database import async_session
from bot.models.moderation import Blacklist, ContentFlag

# Basic banned word patterns (expandable)
BANNED_PATTERNS = [
    r"\b(spam|scam|hack|crack)\b",
    r"(https?://\S+){3,}",  # 3+ URLs = likely spam
    r"(.)\1{10,}",  # Repeated character spam
]

COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in BANNED_PATTERNS]


async def is_blacklisted(user_id: int) -> bool:
    """Check if a user is blacklisted."""
    async with async_session() as session:
        result = await session.execute(
            select(Blacklist).where(
                Blacklist.user_id == user_id,
                Blacklist.is_active == True,
            )
        )
        return result.scalar_one_or_none() is not None


async def add_to_blacklist(user_id: int, banned_by: int, username: str = None, reason: str = None) -> None:
    """Add a user to the blacklist."""
    async with async_session() as session:
        # Check if already blacklisted
        result = await session.execute(
            select(Blacklist).where(Blacklist.user_id == user_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.is_active = True
            existing.reason = reason
            existing.banned_by = banned_by
        else:
            entry = Blacklist(
                user_id=user_id,
                username=username,
                reason=reason,
                banned_by=banned_by,
            )
            session.add(entry)
        await session.commit()


async def remove_from_blacklist(user_id: int) -> bool:
    """Remove a user from the blacklist. Returns True if found and removed."""
    async with async_session() as session:
        result = await session.execute(
            select(Blacklist).where(Blacklist.user_id == user_id, Blacklist.is_active == True)
        )
        entry = result.scalar_one_or_none()
        if entry:
            entry.is_active = False
            await session.commit()
            return True
        return False


def check_content(text: str) -> tuple[bool, str]:
    """Check text content for violations. Returns (is_clean, reason)."""
    if not text:
        return True, ""

    for pattern in COMPILED_PATTERNS:
        if pattern.search(text):
            return False, f"Content flagged: matched pattern '{pattern.pattern}'"

    # Length check
    if len(text) > 5000:
        return False, "Content too long (max 5000 characters)"

    return True, ""


async def flag_content(user_id: int, content_type: str, content_text: str, reason: str) -> None:
    """Flag content for review."""
    async with async_session() as session:
        flag = ContentFlag(
            user_id=user_id,
            content_type=content_type,
            content_text=content_text[:1000] if content_text else None,
            reason=reason,
        )
        session.add(flag)
        await session.commit()
