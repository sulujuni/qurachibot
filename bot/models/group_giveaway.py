"""Group comment-based giveaway model."""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey

from bot.models.base import Base


class GroupGiveawayStatus(enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class GroupGiveawayMode(enum.Enum):
    RANDOM = "random"           # Pick random commenter
    FIRST_N = "first_n"        # First N commenters win
    KEYWORD = "keyword"         # Must include keyword
    REACTION = "reaction"       # Must react to the post


class GroupGiveaway(Base):
    """A group comment-based giveaway — users reply to a post to enter."""

    __tablename__ = "group_giveaways"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    prize: Mapped[str] = mapped_column(String(500), nullable=True)
    winner_count: Mapped[int] = mapped_column(Integer, default=1)
    mode: Mapped[GroupGiveawayMode] = mapped_column(
        Enum(GroupGiveawayMode), default=GroupGiveawayMode.RANDOM
    )
    status: Mapped[GroupGiveawayStatus] = mapped_column(
        Enum(GroupGiveawayStatus), default=GroupGiveawayStatus.ACTIVE
    )

    # Rules
    keyword: Mapped[str] = mapped_column(String(100), nullable=True)  # Required keyword in comment
    one_comment_only: Mapped[bool] = mapped_column(Boolean, default=True)  # Only first comment counts
    min_comment_length: Mapped[int] = mapped_column(Integer, default=1)  # Min characters in comment
    # Comma-separated list of channels users must join to participate (forced-sub)
    required_channels: Mapped[str] = mapped_column(Text, nullable=True)

    # Post-based creation: the admin's original post content
    post_text: Mapped[str] = mapped_column(Text, nullable=True)
    post_file_id: Mapped[str] = mapped_column(String(500), nullable=True)
    post_media_type: Mapped[str] = mapped_column(String(20), nullable=True)  # photo, video, animation, document

    # Telegram message tracking
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    creator_username: Mapped[str] = mapped_column(String(255), nullable=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=True)  # The giveaway post message ID
    is_channel_post: Mapped[bool] = mapped_column(Boolean, default=False)  # Posted in a channel

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    drawn_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    # Relationships
    entries: Mapped[list["GroupGiveawayEntry"]] = relationship(
        back_populates="giveaway", cascade="all, delete-orphan"
    )
    winners: Mapped[list["GroupGiveawayWinner"]] = relationship(
        back_populates="giveaway", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<GroupGiveaway(id={self.id}, title='{self.title}', mode={self.mode.value})>"


class GroupGiveawayEntry(Base):
    """A user's comment entry in a group giveaway."""

    __tablename__ = "group_giveaway_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("group_giveaways.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=True)
    comment_text: Mapped[str] = mapped_column(Text, nullable=True)
    comment_message_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)  # Passes rules check
    entered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    giveaway: Mapped["GroupGiveaway"] = relationship(back_populates="entries")

    def __repr__(self) -> str:
        return f"<GroupGiveawayEntry(user={self.user_id}, giveaway={self.giveaway_id})>"


class GroupGiveawayWinner(Base):
    """A winner from a group giveaway."""

    __tablename__ = "group_giveaway_winners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("group_giveaways.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=True)
    won_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    giveaway: Mapped["GroupGiveaway"] = relationship(back_populates="winners")

    def __repr__(self) -> str:
        return f"<GroupGiveawayWinner(user={self.user_id}, giveaway={self.giveaway_id})>"
