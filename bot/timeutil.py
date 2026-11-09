"""Local-day helpers. A "day" is a calendar date in the configured time zone."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def local_day(moment: datetime, tz: ZoneInfo) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date().isoformat()


def today(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


def window_start(tz: ZoneInfo, days: int) -> str:
    """First day of a window of `days` days that ends today (inclusive)."""
    return (today(tz) - timedelta(days=days - 1)).isoformat()


def in_hour_window(hour: int, start: int, end: int) -> bool:
    """True if hour is in [start, end). Handles windows that cross midnight, e.g. 23 -> 5."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end
