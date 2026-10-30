from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .schemas import Availability, PriceBand, TimeOfDay

CITY_TIME_ZONE = ZoneInfo("Asia/Kolkata")

# Seven local days: today plus the next six.
SHOW_WINDOW_DAYS = 7

# At or above this share of seats sold a show is fast filling.
FAST_FILLING_RATIO = 0.7


def shift_date(day: date) -> int:
    """Days between today and ``day``, so a valid show date is 0 through 6."""
    return (day - date.today()).days


def show_days() -> list[date]:
    """The seven days a show listing covers, always starting today."""
    today = date.today()
    return [today + timedelta(days=offset) for offset in range(SHOW_WINDOW_DAYS)]


def local_day_start(day: date) -> datetime:
    """The naive UTC instant at which ``day`` starts in the city's time zone."""
    local = datetime.combine(day, time.min, tzinfo=CITY_TIME_ZONE)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def window_bounds() -> tuple[datetime, datetime]:
    """Naive UTC bounds enclosing the seven day show window, end exclusive."""
    days = show_days()
    return local_day_start(days[0]), local_day_start(days[-1] + timedelta(days=1))


def local_start(starts_at: datetime) -> datetime:
    """Read a naive UTC start time as an aware datetime in the city's time zone."""
    return starts_at.replace(tzinfo=timezone.utc).astimezone(CITY_TIME_ZONE)


def availability_for(seats_sold: int, capacity: int) -> Availability:
    """Fast filling once at least 70% of the seats are sold."""
    if capacity > 0 and seats_sold / capacity >= FAST_FILLING_RATIO:
        return Availability.FAST_FILLING
    return Availability.AVAILABLE


def is_late_night(starts_at: datetime) -> bool:
    """True for a show starting after 11 PM in the city's time zone."""
    start = local_start(starts_at)
    return start.hour > 23 or (start.hour == 23 and start.minute > 0)


def is_early_morning(starts_at: datetime) -> bool:
    """True for a show starting strictly before 9 AM in the city's time zone."""
    return local_start(starts_at).time() < time(9, 0)


# The fixed price bands the price filter offers, as inclusive (min, max) rupees.
PRICE_BANDS: tuple[tuple[int, int], ...] = ((0, 200), (201, 250), (251, 10000))


def time_of_day(starts_at: datetime) -> TimeOfDay:
    """Bucket a show start by the city's local wall clock.

    Morning is before 12:00, afternoon runs to 16:00, evening to 20:00 and
    night covers the rest of the day, so 20:00 exactly is night.
    """
    start = local_start(starts_at)
    if start.hour < 12:
        return TimeOfDay.MORNING
    if start.hour < 16:
        return TimeOfDay.AFTERNOON
    if start.hour < 20:
        return TimeOfDay.EVENING
    return TimeOfDay.NIGHT


def price_bands_for(prices: Iterable[Decimal]) -> list[PriceBand]:
    """The fixed price bands that hold at least one of ``prices``."""
    values = list(prices)
    return [
        PriceBand(min=low, max=high)
        for low, high in PRICE_BANDS
        if any(low <= value <= high for value in values)
    ]
