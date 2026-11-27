"""Plain words for times, shared by every page: "just now", "5 minutes ago"."""

from __future__ import annotations

from datetime import UTC, datetime


def seconds_of(value):
    """A time as epoch seconds: an aware or naive (UTC) datetime, an ISO string or a number. None when unreadable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
    return None


def ago(stamp, now):
    """Plain, coarse time since `stamp`, such as "5 minutes ago". "" for an unreadable time.

    Both times can be datetimes, ISO strings or epoch seconds. Under 10 seconds is "just now".
    """
    moment, current = seconds_of(stamp), seconds_of(now)
    if moment is None or current is None:
        return ""
    seconds = current - moment
    if seconds < 10:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


def how_long(seconds):
    """How long something has lasted, for "Room empty for 12 min". "" under a minute.

    Under an hour: whole minutes, such as "12 min". From an hour: hours and minutes rounded down to
    5 minutes, such as "2 hours", "1 hour" or "7 h 30 min".
    """
    if seconds is None or seconds < 60:
        return ""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    rest -= rest % 5
    if not rest:
        return f"{hours} hour{'' if hours == 1 else 's'}"
    return f"{hours} h {rest} min"
