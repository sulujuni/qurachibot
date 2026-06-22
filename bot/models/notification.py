"""Notification and subscription models."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.models.base import Base


class AlertSubscription(Base):
    """Users subscribed to new giveaway/contest alerts."""

    __tablename__ = "alert_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(255), nullable=True)
    notify_giveaways: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_contests: Mapped[bool] = mapped_column(Boolean, default=True)
    subscribed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def __repr__(self) -> str:
        return f"<AlertSubscription(user_id={self.user_id})>"


class ScheduledReminder(Base):
    """Scheduled reminder for giveaway/contest deadlines."""

    __tablename__ = "scheduled_reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "giveaway" or "contest"
    event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remind_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def __repr__(self) -> str:
        return f"<ScheduledReminder(type={self.event_type}, id={self.event_id}, at={self.remind_at})>"
