from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import Cinema, City, Movie, Screen, Show
from ..schemas import (
    AppliedFiltersOut,
    AvailableFiltersOut,
    CinemaAmenity,
    CinemaShowsOut,
    MovieDetailOut,
    MovieFormat,
    MovieOut,
    MovieShowsOut,
    NonBlank,
    ShowMovieOut,
    ShowOut,
    ShowSort,
    ShowsDateOut,
    TimeOfDay,
)
from ..showtime import (
    availability_for,
    is_early_morning,
    is_late_night,
    local_day_start,
    local_start,
    price_bands_for,
    show_days,
    shift_date,
    time_of_day,
    window_bounds,
)

router = APIRouter(prefix="/api/movies", tags=["movies"])


def _to_summary(movie: Movie) -> MovieOut:
    return MovieOut(
        id=movie.id,
        title=movie.title,
        slug=movie.slug,
        genres=movie.genres,
        posterUrl=movie.poster_url,
        rating=movie.rating,
        votes=movie.votes,
        likes=movie.likes,
        promoted=movie.promoted,
    )


def _to_detail(movie: Movie) -> MovieDetailOut:
    return MovieDetailOut(
        id=movie.id,
        title=movie.title,
        slug=movie.slug,
        genres=movie.genres,
        posterUrl=movie.poster_url,
        rating=movie.rating,
        votes=movie.votes,
        likes=movie.likes,
        promoted=movie.promoted,
        synopsis=movie.synopsis,
        languages=movie.languages,
        formats=movie.formats,
        durationMinutes=movie.duration_minutes,
        certificate=movie.certificate,
        releaseDate=movie.release_date,
        backdropUrl=movie.backdrop_url,
        trailerUrl=movie.trailer_url,
        interestedCount=movie.interested_count,
    )


@router.get("", response_model=list[MovieOut])
def list_movies(
    city: NonBlank | None = None, db: Session = Depends(get_db)
) -> list[MovieOut]:
    if city is None:
        movies = db.scalars(select(Movie).order_by(Movie.id))
        return [_to_summary(movie) for movie in movies]

    start, end = window_bounds()
    movies = db.scalars(
        select(Movie)
        .join(Show, Show.movie_id == Movie.id)
        .join(Screen, Show.screen_id == Screen.id)
        .join(Cinema, Screen.cinema_id == Cinema.id)
        .join(City, Cinema.city_id == City.id)
        .where(
            func.lower(City.slug) == city.lower(),
            Show.starts_at >= start,
            Show.starts_at < end,
        )
        .distinct()
        .order_by(Movie.id)
    )
    return [_to_summary(movie) for movie in movies]


@router.get("/{movie_id}", response_model=MovieDetailOut)
def get_movie(movie_id: int, db: Session = Depends(get_db)) -> MovieDetailOut:
    movie = db.get(Movie, movie_id)
    if movie is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Movie not found: {movie_id}",
        )
    return _to_detail(movie)


def _shows_query(db: Session, movie_id: int, city: str, language: str) -> Select:
    start, end = window_bounds()
    return (
        select(Show)
        .options(
            selectinload(Show.screen).selectinload(Screen.seat_categories),
            selectinload(Show.screen).selectinload(Screen.cinema),
        )
        .join(Screen, Show.screen_id == Screen.id)
        .join(Cinema, Screen.cinema_id == Cinema.id)
        .join(City, Cinema.city_id == City.id)
        .where(
            Show.movie_id == movie_id,
            func.lower(City.slug) == city.lower(),
            func.lower(Show.language) == language.lower(),
            Show.starts_at >= start,
            Show.starts_at < end,
        )
        .order_by(Show.starts_at, Show.id)
    )


def _to_show(show: Show) -> ShowOut:
    return ShowOut(
        id=show.id,
        startsAt=local_start(show.starts_at),
        format=show.format,
        language=show.language,
        availability=availability_for(show.seats_sold, show.capacity),
        lateNight=is_late_night(show.starts_at),
        earlyMorning=is_early_morning(show.starts_at),
    )


@dataclass(frozen=True)
class ShowFilters:
    """The show filters a request asked for, already validated."""

    price_min: int | None
    price_max: int | None
    time_of_day: frozenset[TimeOfDay]
    format: MovieFormat | None
    cancellable: bool | None
    wheelchair: bool | None
    sort: ShowSort

    def is_unfiltered(self) -> bool:
        """True when nothing but the sort order was supplied."""
        return (
            self.price_min is None
            and self.price_max is None
            and not self.time_of_day
            and self.format is None
            and self.cancellable is None
            and self.wheelchair is None
        )


EnumT = TypeVar("EnumT", bound=Enum)


def _parse_enum(enum_type: type[EnumT], value: str, parameter: str) -> EnumT:
    try:
        return enum_type(value.strip().upper())
    except ValueError:
        allowed = ", ".join(member.value for member in enum_type)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{parameter} must be one of: {allowed}",
        ) from None


def _parse_filters(
    price_min: int | None,
    price_max: int | None,
    time_of_day: list[str],
    format: str | None,
    cancellable: bool | None,
    wheelchair: bool | None,
    sort: str | None,
) -> ShowFilters:
    if (
        price_min is not None
        and price_max is not None
        and price_max < price_min
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="priceMax must be greater than or equal to priceMin",
        )

    return ShowFilters(
        price_min=price_min,
        price_max=price_max,
        time_of_day=frozenset(
            _parse_enum(TimeOfDay, value, "timeOfDay") for value in time_of_day
        ),
        format=None if format is None else _parse_enum(MovieFormat, format, "format"),
        cancellable=cancellable,
        wheelchair=wheelchair,
        sort=ShowSort.NAME if sort is None else _parse_enum(ShowSort, sort, "sort"),
    )


def _matches_filters(show: Show, filters: ShowFilters) -> bool:
    if filters.is_unfiltered():
        return True

    if filters.time_of_day and time_of_day(show.starts_at) not in filters.time_of_day:
        return False

    if (
        filters.format is not None
        and show.format.strip().upper() != filters.format.value
    ):
        return False

    cinema = show.screen.cinema
    if filters.cancellable is not None and cinema.cancellable != filters.cancellable:
        return False
    if filters.wheelchair is not None:
        accessible = CinemaAmenity.WHEELCHAIR.value in cinema.amenities
        if accessible != filters.wheelchair:
            return False

    if filters.price_min is not None or filters.price_max is not None:
        return any(
            (filters.price_min is None or category.price >= filters.price_min)
            and (filters.price_max is None or category.price <= filters.price_max)
            for category in show.screen.seat_categories
        )

    return True


def _applied_filters(filters: ShowFilters) -> AppliedFiltersOut:
    return AppliedFiltersOut(
        priceMin=filters.price_min,
        priceMax=filters.price_max,
        timeOfDay=[bucket for bucket in TimeOfDay if bucket in filters.time_of_day],
        format=filters.format,
        cancellable=filters.cancellable,
        wheelchair=filters.wheelchair,
        sort=filters.sort,
    )


def _available_filters(shows: Iterable[Show]) -> AvailableFiltersOut:
    """The price bands and formats a client can offer for the unfiltered day.

    A format outside the declared 2D/3D/4DX/IMAX set is left out: the response
    types formats as MovieFormat, so such a value could never be requested back.
    """
    declared_formats = {member.value: member for member in MovieFormat}
    formats: set[MovieFormat] = set()
    prices: set[Decimal] = set()
    for show in shows:
        prices.update(category.price for category in show.screen.seat_categories)
        declared = declared_formats.get(show.format.strip().upper())
        if declared is not None:
            formats.add(declared)

    return AvailableFiltersOut(
        priceBands=price_bands_for(prices),
        formats=sorted(formats, key=lambda value: value.value),
    )


@router.get("/{movie_id}/shows", response_model=MovieShowsOut)
def get_movie_shows(
    movie_id: int,
    city: NonBlank,
    date: date,
    language: NonBlank | None = None,
    price_min: int | None = Query(None, alias="priceMin"),
    price_max: int | None = Query(None, alias="priceMax"),
    time_of_day: list[str] = Query(default_factory=list, alias="timeOfDay"),
    format: str | None = None,
    cancellable: bool | None = None,
    wheelchair: bool | None = None,
    sort: str | None = None,
    db: Session = Depends(get_db),
) -> MovieShowsOut:
    movie = db.get(Movie, movie_id)
    if movie is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Movie not found: {movie_id}",
        )

    # A missing language is a bad request rather than FastAPI's 422 so the client
    # gets the same error shape as the date window check below.
    if language is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="language is required",
        )

    days = show_days()
    if not 0 <= shift_date(date) < len(days):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date must be today or within the next 6 days",
        )

    # The filters arrive as plain strings and are validated here because an
    # unknown value is a 400 in the contract, while FastAPI's own enum
    # validation would answer 422.
    filters = _parse_filters(
        price_min, price_max, time_of_day, format, cancellable, wheelchair, sort
    )

    shows = list(db.scalars(_shows_query(db, movie_id, city, language)))

    days_with_shows = {local_start(show.starts_at).date() for show in shows}
    dates = [
        ShowsDateOut(date=day, hasShows=day in days_with_shows) for day in days
    ]

    day_start = local_day_start(date)
    day_end = local_day_start(date + timedelta(days=1))
    day_shows = [show for show in shows if day_start <= show.starts_at < day_end]

    grouped: dict[int, list[Show]] = {}
    for show in day_shows:
        if _matches_filters(show, filters):
            grouped.setdefault(show.screen.cinema_id, []).append(show)

    cinemas = list(db.scalars(select(Cinema).where(Cinema.id.in_(list(grouped)))))
    if filters.sort is ShowSort.EARLIEST:
        cinemas.sort(
            key=lambda cinema: min(show.starts_at for show in grouped[cinema.id])
        )
    else:
        cinemas.sort(key=lambda cinema: (cinema.name.lower(), cinema.id))

    return MovieShowsOut(
        movie=ShowMovieOut(
            id=movie.id,
            title=movie.title,
            durationMinutes=movie.duration_minutes,
            certificate=movie.certificate,
            genres=movie.genres,
        ),
        dates=dates,
        cinemas=[
            CinemaShowsOut(
                id=cinema.id,
                name=cinema.name,
                address=cinema.address,
                chainLogoUrl=cinema.chain_logo_url,
                cancellable=cinema.cancellable,
                amenities=cinema.amenities,
                shows=[_to_show(show) for show in grouped[cinema.id]],
            )
            for cinema in cinemas
        ],
        appliedFilters=_applied_filters(filters),
        availableFilters=_available_filters(day_shows),
    )
