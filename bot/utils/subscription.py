"""Channel subscription verification (forced-sub / majburiy obuna).

Lets a giveaway/contest require users to be subscribed to one or more
channels before they can participate. The bot must be a member (ideally
admin) of each required channel for membership checks to work.
"""

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

logger = logging.getLogger(__name__)

# Telegram chat member statuses that count as "subscribed"
_MEMBER_STATUSES = {"creator", "administrator", "member"}


def parse_channels(raw: str | None) -> list[str]:
    """Parse a stored/entered channel string into a clean list.

    Accepts comma/space/newline separated channel references such as
    "@channel1, @channel2" or "https://t.me/channel3". Returns a list of
    normalized references (usernames prefixed with @, or raw IDs/links).
    """
    if not raw:
        return []

    channels: list[str] = []
    for token in raw.replace("\n", ",").replace(" ", ",").split(","):
        token = token.strip()
        if not token:
            continue
        # Normalize t.me links to @username
        for prefix in ("https://t.me/", "http://t.me/", "t.me/", "telegram.me/"):
            if token.lower().startswith(prefix):
                token = "@" + token[len(prefix):]
                break
        # Ensure a leading @ for bare usernames (but keep numeric -100... IDs)
        if not token.startswith("@") and not token.lstrip("-").isdigit():
            token = "@" + token
        channels.append(token)
    return channels


def serialize_channels(channels: list[str]) -> str | None:
    """Serialize a list of channels into a comma-separated string for storage."""
    cleaned = [c.strip() for c in channels if c and c.strip()]
    return ",".join(cleaned) if cleaned else None


async def is_member(bot: Bot, channel: str, user_id: int) -> bool:
    """Check whether a user is a member of a single channel.

    Returns True on membership. On error (e.g. bot not admin, channel not
    found) returns True to avoid unfairly blocking users due to a
    misconfiguration — the failure is logged instead.
    """
    chat_id: str | int = channel
    if isinstance(channel, str) and channel.lstrip("-").isdigit():
        chat_id = int(channel)

    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramError as e:
        logger.warning("Subscription check failed for %s (user %s): %s", channel, user_id, e)
        return True  # fail-open on misconfiguration

    status = getattr(member, "status", None)
    if status in _MEMBER_STATUSES:
        return True
    # "restricted" users may still be members
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def get_unsubscribed(bot: Bot, user_id: int, channels: list[str]) -> list[str]:
    """Return the list of channels the user is NOT subscribed to."""
    missing: list[str] = []
    for channel in channels:
        if not await is_member(bot, channel, user_id):
            missing.append(channel)
    return missing


def _channel_url(channel: str) -> str | None:
    """Build a public URL for a channel reference, if possible."""
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}"
    if channel.startswith("http"):
        return channel
    return None


def build_subscription_keyboard(
    channels: list[str],
    retry_callback: str,
    join_label: str = "📢 {channel}",
    check_label: str = "✅ I've subscribed",
) -> InlineKeyboardMarkup:
    """Build a keyboard listing each channel plus a re-check button.

    `retry_callback` is the callback_data fired when the user taps the
    "I've subscribed" button so the caller can re-run verification.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for channel in channels:
        url = _channel_url(channel)
        label = join_label.format(channel=channel)
        if url:
            rows.append([InlineKeyboardButton(label, url=url)])
        else:
            rows.append([InlineKeyboardButton(label, callback_data="noop")])
    rows.append([InlineKeyboardButton(check_label, callback_data=retry_callback)])
    return InlineKeyboardMarkup(rows)
