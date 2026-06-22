"""Referral system models."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.models.base import Base


class Referral(Base):
    """Tracks referral links and who referred whom."""

    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    referred_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    giveaway_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("giveaways.id", ondelete="CASCADE"), nullable=True
    )
    referrer_username: Mapped[str] = mapped_column(String(255), nullable=True)
    referred_username: Mapped[str] = mapped_column(String(255), nullable=True)
    bonus_entries: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def __repr__(self) -> str:
        return f"<Referral(referrer={self.referrer_id}, referred={self.referred_id})>"
