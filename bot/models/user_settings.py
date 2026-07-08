"""User settings model for storing language preferences."""

from sqlalchemy import BigInteger, Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from bot.models.base import Base


class UserSettings(Base):
    """Stores per-user settings like language preference."""

    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(5), default="en", nullable=False)
    captcha_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    gender: Mapped[str] = mapped_column(String(10), nullable=True)  # 'male', 'female', or None

    def __repr__(self) -> str:
        return f"<UserSettings(user_id={self.user_id}, language='{self.language}', verified={self.captcha_verified}, gender={self.gender})>"
