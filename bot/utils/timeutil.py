"""Timezone helpers.

All datetimes are stored in the DB as naive UTC. Users enter and see times
in the local timezone configured via TIMEZONE (default Asia/Tashkent).
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.config import settings

LOCAL_TZ = ZoneInfo(settings.TIMEZONE)

USER_DT_FORMAT = "%Y-%m-%d %H:%M"


def local_to_utc(dt: datetime) -> datetime:
    """Naive local datetime → naive UTC datetime."""
    return dt.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def utc_to_local(dt: datetime) -> datetime:
    """Naive UTC datetime → naive local datetime."""
    return dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).replace(tzinfo=None)


def parse_user_datetime(text: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM' entered by a user (local time) → naive UTC.

    Raises ValueError on bad format.
    """
    return local_to_utc(datetime.strptime(text.strip(), USER_DT_FORMAT))


def parse_iso_to_utc(text: str) -> datetime:
    """Parse an ISO string (from the Mini App) → naive UTC.

    Accepts 'Z' / '+05:00' offsets; a naive value is assumed to be local time
    (older clients sent raw datetime-local values).
    """
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def fmt_local(dt: datetime | None, default: str = "") -> str:
    """Format a naive-UTC datetime for display in local time."""
    if not dt:
        return default
    return utc_to_local(dt).strftime(USER_DT_FORMAT)


def iso_utc(dt: datetime | None) -> str | None:
    """Naive-UTC datetime → ISO string with explicit Z (for the Mini App)."""
    return dt.isoformat() + "Z" if dt else None
