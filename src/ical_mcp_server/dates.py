"""Date parsing helpers. All naive datetimes are treated as server-local time."""

from __future__ import annotations

from datetime import date, datetime, time


def parse_dt(value: str) -> datetime:
    """Parse 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM' or ISO 8601 into a naive local datetime."""
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"Invalid date/time {value!r}. Use 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' (server local time)."
        ) from None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def is_date_only(value: str) -> bool:
    value = value.strip()
    try:
        date.fromisoformat(value)
        return len(value) == 10
    except ValueError:
        return False


def start_of_day(d: datetime) -> datetime:
    return datetime.combine(d.date(), time.min)


def icalbuddy_dt(dt: datetime) -> str:
    """Format for icalBuddy's eventsFrom:/to: arguments (explicit local offset)."""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
