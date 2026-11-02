from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Cinema, City, Movie, Screen, SeatCategory, Show, ShowSeat
from ..schemas import (
    AppliedFiltersOut,
    Availability,
    AvailableFiltersOut,
    MovieShowsOut,
    PriceBandOut,
    ShowCinemaOut,
    ShowDateOut,
    ShowMovieOut,
    ShowOut,
    ShowSeatStatus,
    ShowSort,
    TimeOfDay,
)
from ..timeutil import ist_local, today_ist, utc_from_ist

router = APIRouter(prefix="/api/movies", tags=["shows"])

SHOW_WINDOW_DAYS = 7
FAST_FILLING_THRESHOLD = 0.5
LATE_NIGHT_CUTOFF = (23, 0)
EARLY_MORNING_CUTOFF = (9, 0)
SHOW_DATE_DETAIL = "date must be today or within the next 6 days"
PRICE_ORDER_DETAIL = "priceMax must be greater than or equal to priceMin"
TIME_OF_DAY_DETAIL = "timeOfDay must be one of MORNING, AFTERNOON, EVENING, NIGHT"
SORT_DETAIL = "sort must be one of NAME, EARLIEST"
PRICE_BANDS: list[tuple[int, int | None]] = [(0, 200), (201, 250), (251, None)]


def _parse_show_date(raw: str | None) -> date:
    today = today_ist()
    last_day = today + timedelta(days=SHOW_WINDOW_DAYS - 1)
    if raw is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=SHOW_DATE_DETAIL)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=SHOW_DATE_DETAIL
        ) from None
    if parsed < today or parsed > last_day:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=SHOW_DATE_DETAIL)
    return parsed


def _parse_int_param(param: str, raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{param} must be an integer",
        ) from None


def _parse_price_range(
    price_min: str | None, price_max: str | None
) -> tuple[int | None, int | None]:
    low = _parse_int_param("priceMin", price_min)
    high = _parse_int_param("priceMax", price_max)
    if low is not None and high is not None and high < low:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=PRICE_ORDER_DETAIL
        )
    return low, high


def _parse_time_of_day(raw: list[str]) -> list[TimeOfDay]:
    parsed: list[TimeOfDay] = []
    for value in raw:
        candidate = value.strip().upper()
        if not candidate:
            continue
        try:
            parsed.append(TimeOfDay(candidate))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=TIME_OF_DAY_DETAIL
            ) from None
    return parsed


def _parse_sort(raw: str | None) -> ShowSort:
    if raw is None or not raw.strip():
        return ShowSort.NAME
    try:
        return ShowSort(raw.strip().upper())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=SORT_DETAIL
        ) from None


def _parse_bool_flag(param: str, raw: str | None) -> bool | None:
    if raw is None or not raw.strip():
        return None
    lowered = raw.strip().lower()
    if lowered in {"true", "1"}:
        return True
    if lowered in {"false", "0"}:
        return False
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"{param} must be true or false",
    )


def _time_bucket(local: datetime) -> TimeOfDay:
    if local.hour < 12:
        return TimeOfDay.MORNING
    if local.hour < 16:
        return TimeOfDay.AFTERNOON
    if local.hour < 20:
        return TimeOfDay.EVENING
    return TimeOfDay.NIGHT


def _show_seat_counts(db: Session, show_ids: list[int]) -> dict[int, tuple[int, int]]:
    """Return (held-or-sold, total) ShowSeat counts per show id."""
    counts: dict[int, tuple[int, int]] = {show_id: (0, 0) for show_id in show_ids}
    if not show_ids:
        return counts

    rows = db.execute(
        select(ShowSeat.show_id, ShowSeat.status, func.count(ShowSeat.id))
        .where(ShowSeat.show_id.in_(show_ids))
        .group_by(ShowSeat.show_id, ShowSeat.status)
    )
    for show_id, seat_status, count in rows.all():
        held_or_sold, total = counts[show_id]
        if seat_status != ShowSeatStatus.AVAILABLE.value:
            held_or_sold += count
        counts[show_id] = (held_or_sold, total + count)
    return counts


def _availability(held_or_sold: int, total: int) -> Availability:
    if total > 0 and held_or_sold / total >= FAST_FILLING_THRESHOLD:
        return Availability.FAST_FILLING
    return Availability.AVAILABLE


def _to_show_out(show: Show, held_or_sold: int, total: int) -> ShowOut:
    local = ist_local(show.starts_at)
    clock = (local.hour, local.minute)
    return ShowOut(
        id=show.id,
        startsAt=local,
        format=show.format,
        language=show.language,
        availability=_availability(held_or_sold, total),
        lateNight=clock > LATE_NIGHT_CUTOFF,
        earlyMorning=clock < EARLY_MORNING_CUTOFF,
    )


def _city_movie_shows(
    movie_id: int, city_slug: str, language: str, db: Session
) -> list[tuple[Show, int]]:
    window_start = utc_from_ist(today_ist(), 0, 0)
    window_end = window_start + timedelta(days=SHOW_WINDOW_DAYS)
    rows = db.execute(
        select(Show, Screen.cinema_id)
        .join(Screen, Show.screen_id == Screen.id)
        .join(Cinema, Screen.cinema_id == Cinema.id)
        .join(City, Cinema.city_id == City.id)
        .where(
            Show.movie_id == movie_id,
            Show.language == language,
            City.slug == city_slug,
            Show.starts_at >= window_start,
            Show.starts_at < window_end,
        )
        .order_by(Show.starts_at, Show.id)
    )
    return list(rows.all())


def _screen_seat_prices(db: Session, screen_ids: list[int]) -> dict[int, list[Decimal]]:
    prices: dict[int, list[Decimal]] = {}
    if not screen_ids:
        return prices
    rows = db.execute(
        select(SeatCategory.screen_id, SeatCategory.price).where(
            SeatCategory.screen_id.in_(screen_ids)
        )
    )
    for screen_id, price in rows.all():
        prices.setdefault(screen_id, []).append(Decimal(price))
    return prices


def _price_bands(prices: set[Decimal]) -> list[PriceBandOut]:
    bands: list[PriceBandOut] = []
    for band_min, band_max in PRICE_BANDS:
        if any(
            price >= band_min and (band_max is None or price <= band_max)
            for price in prices
        ):
            bands.append(PriceBandOut(min=band_min, max=band_max))
    return bands


def _show_matches(
    show: Show, cinema: Cinema, prices: list[Decimal], filters: AppliedFiltersOut
) -> bool:
    if filters.priceMin is not None or filters.priceMax is not None:
        in_range = False
        for price in prices:
            if filters.priceMin is not None and price < filters.priceMin:
                continue
            if filters.priceMax is not None and price > filters.priceMax:
                continue
            in_range = True
            break
        if not in_range:
            return False

    if filters.format and show.format not in filters.format:
        return False

    if filters.timeOfDay and _time_bucket(ist_local(show.starts_at)) not in filters.timeOfDay:
        return False

    if filters.cancellable and not cinema.cancellable:
        return False

    if filters.wheelchair and "WHEELCHAIR" not in (cinema.amenities or []):
        return False

    return True


def _sort_cinemas(cinemas: list[ShowCinemaOut], sort: ShowSort) -> list[ShowCinemaOut]:
    if sort is ShowSort.EARLIEST:
        return sorted(
            cinemas,
            key=lambda cinema: (cinema.shows[0].startsAt, cinema.id),
        )
    return sorted(cinemas, key=lambda cinema: (cinema.name, cinema.id))


def _to_cinema_out(cinema: Cinema, shows: list[ShowOut]) -> ShowCinemaOut:
    return ShowCinemaOut(
        id=cinema.id,
        name=cinema.name,
        address=cinema.address,
        chainLogoUrl=cinema.chain_logo_url,
        cancellable=cinema.cancellable,
        amenities=cinema.amenities,
        shows=shows,
    )


def _date_strip(days_with_shows: set[date]) -> list[ShowDateOut]:
    today = today_ist()
    return [
        ShowDateOut(
            date=day,
            hasShows=day in days_with_shows,
        )
        for day in (today + timedelta(days=offset) for offset in range(SHOW_WINDOW_DAYS))
    ]


@router.get("/{movie_id}/shows", response_model=MovieShowsOut)
def list_shows(
    movie_id: int,
    city: str,
    date: str | None = None,
    language: str | None = None,
    priceMin: str | None = None,
    priceMax: str | None = None,
    timeOfDay: list[str] = Query(default=[]),
    format: list[str] = Query(default=[]),
    cancellable: str | None = None,
    wheelchair: str | None = None,
    sort: str | None = None,
    db: Session = Depends(get_db),
) -> MovieShowsOut:
    movie = db.get(Movie, movie_id)
    if movie is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Movie not found: {movie_id}",
        )

    selected = _parse_show_date(date)

    if language is None or not language.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="language is required",
        )
    language = language.strip().lower()

    lowest_price, highest_price = _parse_price_range(priceMin, priceMax)
    filters = AppliedFiltersOut(
        priceMin=lowest_price,
        priceMax=highest_price,
        timeOfDay=_parse_time_of_day(timeOfDay),
        format=[value.strip() for value in format if value.strip()],
        cancellable=_parse_bool_flag("cancellable", cancellable),
        wheelchair=_parse_bool_flag("wheelchair", wheelchair),
        sort=_parse_sort(sort),
    )

    shows_by_cinema: dict[int, list[Show]] = {}
    days_with_shows = set()
    for show, cinema_id in _city_movie_shows(movie_id, city, language, db):
        day = ist_local(show.starts_at).date()
        days_with_shows.add(day)
        if day != selected:
            continue
        shows_by_cinema.setdefault(cinema_id, []).append(show)

    day_shows = [show for shows in shows_by_cinema.values() for show in shows]
    seat_counts = _show_seat_counts(db, [show.id for show in day_shows])
    seat_prices = _screen_seat_prices(db, list({show.screen_id for show in day_shows}))
    available_filters = AvailableFiltersOut(
        priceBands=_price_bands(
            {price for prices in seat_prices.values() for price in prices}
        ),
        formats=sorted({show.format for show in day_shows}),
    )

    cinemas: list[ShowCinemaOut] = []
    if shows_by_cinema:
        for cinema in db.scalars(
            select(Cinema).where(Cinema.id.in_(list(shows_by_cinema))).order_by(Cinema.id)
        ):
            matching_shows = [
                _to_show_out(show, *seat_counts.get(show.id, (0, 0)))
                for show in shows_by_cinema[cinema.id]
                if _show_matches(show, cinema, seat_prices.get(show.screen_id, []), filters)
            ]
            if not matching_shows:
                continue
            cinemas.append(_to_cinema_out(cinema, matching_shows))

    return MovieShowsOut(
        movie=ShowMovieOut(
            id=movie.id,
            title=movie.title,
            durationMinutes=movie.duration_minutes,
            certificate=movie.certificate,
            genres=movie.genres,
        ),
        dates=_date_strip(days_with_shows),
        cinemas=_sort_cinemas(cinemas, filters.sort),
        appliedFilters=filters,
        availableFilters=available_filters,
    )
