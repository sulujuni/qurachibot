"""Database models for the giveaway bot."""

from bot.models.base import Base
from bot.models.contest import Contest, ContestStatus, ContestSubmission, ContestType, ContestVote
from bot.models.database import async_session, engine, init_db, close_db
from bot.models.giveaway import Giveaway, GiveawayParticipant, GiveawayStatus, GiveawayWinner
from bot.models.group_giveaway import (
    GroupGiveaway, GroupGiveawayEntry, GroupGiveawayMode,
    GroupGiveawayStatus, GroupGiveawayWinner,
)
from bot.models.loyalty import LoyaltyPoints, PointsTransaction
from bot.models.moderation import Blacklist, ContentFlag, RateLimitLog
from bot.models.notification import AlertSubscription, ScheduledReminder
from bot.models.referral import Referral
from bot.models.user_settings import UserSettings

__all__ = [
    "Base",
    "Contest", "ContestStatus", "ContestSubmission", "ContestType", "ContestVote",
    "Giveaway", "GiveawayParticipant", "GiveawayStatus", "GiveawayWinner",
    "LoyaltyPoints", "PointsTransaction",
    "Blacklist", "ContentFlag", "RateLimitLog",
    "AlertSubscription", "ScheduledReminder",
    "Referral",
    "UserSettings",
    "GroupGiveaway", "GroupGiveawayEntry", "GroupGiveawayMode",
    "GroupGiveawayStatus", "GroupGiveawayWinner",
    "async_session", "engine", "init_db", "close_db",
]
