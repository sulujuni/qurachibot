"""Moderation, blacklist, and rate limiting models."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.models.base import Base


class Blacklist(Base):
    """Blacklisted users who cannot participate."""

    __tablename__ = "blacklist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(255), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=True)
    banned_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    banned_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    def __repr__(self) -> str:
        return f"<Blacklist(user_id={self.user_id}, active={self.is_active})>"


class RateLimitLog(Base):
    """Tracks user actions for rate limiting."""

    __tablename__ = "rate_limit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. "join", "submit", "vote"
    timestamp: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def __repr__(self) -> str:
        return f"<RateLimitLog(user={self.user_id}, action={self.action})>"


class ContentFlag(Base):
    """Flagged content from moderation checks."""

    __tablename__ = "content_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)  # "submission", "text"
    content_text: Mapped[str] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    flagged_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    def __repr__(self) -> str:
        return f"<ContentFlag(user={self.user_id}, reason={self.reason})>"
