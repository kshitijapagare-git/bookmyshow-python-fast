from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

INDIA_TZ = ZoneInfo("Asia/Kolkata")


def utc_from_ist(day: date, hour: int, minute: int = 0) -> datetime:
    """Build a UTC instant from an Asia/Kolkata wall-clock time."""
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=INDIA_TZ)
    return local.astimezone(timezone.utc)


def today_ist() -> date:
    return datetime.now(INDIA_TZ).date()


def ist_local(moment: datetime) -> datetime:
    """Convert a stored UTC instant to a tz-aware Asia/Kolkata datetime."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(INDIA_TZ)
