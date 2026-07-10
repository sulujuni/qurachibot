"""Giveaway and participant models."""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.models.base import Base


class GiveawayStatus(enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Giveaway(Base):
    """A giveaway event created by an admin/organizer."""

    __tablename__ = "giveaways"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    prize: Mapped[str] = mapped_column(String(500), nullable=True)
    winner_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[GiveawayStatus] = mapped_column(
        Enum(GiveawayStatus), default=GiveawayStatus.ACTIVE
    )
    # Comma-separated list of channels users must join to participate (forced-sub)
    required_channels: Mapped[str] = mapped_column(Text, nullable=True)

    # Post-based creation: the admin's original post content
    post_text: Mapped[str] = mapped_column(Text, nullable=True)
    post_file_id: Mapped[str] = mapped_column(String(500), nullable=True)
    post_media_type: Mapped[str] = mapped_column(String(20), nullable=True)  # photo, video, animation, document

    # Test mode: if True, does not notify subscribers
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)

    # Telegram info
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    creator_username: Mapped[str] = mapped_column(String(255), nullable=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    drawn_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    # Relationships
    participants: Mapped[list["GiveawayParticipant"]] = relationship(
        back_populates="giveaway", cascade="all, delete-orphan"
    )
    winners: Mapped[list["GiveawayWinner"]] = relationship(
        back_populates="giveaway", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Giveaway(id={self.id}, title='{self.title}', status={self.status.value})>"


class GiveawayParticipant(Base):
    """A user who joined a giveaway."""

    __tablename__ = "giveaway_participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("giveaways.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    giveaway: Mapped["Giveaway"] = relationship(back_populates="participants")

    def __repr__(self) -> str:
        return f"<GiveawayParticipant(user_id={self.user_id}, giveaway_id={self.giveaway_id})>"


class GiveawayWinner(Base):
    """A winner selected from a giveaway draw."""

    __tablename__ = "giveaway_winners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("giveaways.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=True)
    won_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    giveaway: Mapped["Giveaway"] = relationship(back_populates="winners")

    def __repr__(self) -> str:
        return f"<GiveawayWinner(user_id={self.user_id}, giveaway_id={self.giveaway_id})>"
