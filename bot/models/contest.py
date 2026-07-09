"""Contest, submission, and vote models."""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.models.base import Base


class ContestStatus(enum.Enum):
    ACCEPTING_SUBMISSIONS = "accepting_submissions"
    VOTING = "voting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ContestType(enum.Enum):
    TEXT = "text"
    PHOTO = "photo"
    ANY = "any"


class Contest(Base):
    """A contest where users submit entries and others vote."""

    __tablename__ = "contests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    prize: Mapped[str] = mapped_column(String(500), nullable=True)
    contest_type: Mapped[ContestType] = mapped_column(
        Enum(ContestType), default=ContestType.ANY
    )
    status: Mapped[ContestStatus] = mapped_column(
        Enum(ContestStatus), default=ContestStatus.ACCEPTING_SUBMISSIONS
    )
    max_submissions_per_user: Mapped[int] = mapped_column(Integer, default=1)
    winner_count: Mapped[int] = mapped_column(Integer, default=1)

    # Post-based creation: the admin's original post content
    post_text: Mapped[str] = mapped_column(Text, nullable=True)
    post_file_id: Mapped[str] = mapped_column(String(500), nullable=True)
    post_media_type: Mapped[str] = mapped_column(String(20), nullable=True)  # photo, video, animation, document

    # Telegram info
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    creator_username: Mapped[str] = mapped_column(String(255), nullable=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    submissions_end_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    voting_end_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    # Relationships
    submissions: Mapped[list["ContestSubmission"]] = relationship(
        back_populates="contest", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Contest(id={self.id}, title='{self.title}', status={self.status.value})>"


class ContestSubmission(Base):
    """A submission to a contest."""

    __tablename__ = "contest_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contest_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("contests.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=True)

    # Submission content
    text_content: Mapped[str] = mapped_column(Text, nullable=True)
    file_id: Mapped[str] = mapped_column(String(500), nullable=True)
    caption: Mapped[str] = mapped_column(Text, nullable=True)

    # Vote tracking
    vote_count: Mapped[int] = mapped_column(Integer, default=0)

    # Timestamps
    submitted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    contest: Mapped["Contest"] = relationship(back_populates="submissions")
    votes: Mapped[list["ContestVote"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ContestSubmission(id={self.id}, user_id={self.user_id})>"


class ContestVote(Base):
    """A vote on a contest submission."""

    __tablename__ = "contest_votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("contest_submissions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    voted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    submission: Mapped["ContestSubmission"] = relationship(back_populates="votes")

    def __repr__(self) -> str:
        return f"<ContestVote(user_id={self.user_id}, submission_id={self.submission_id})>"
