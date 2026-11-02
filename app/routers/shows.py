from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Cinema, City, Movie, Screen, Show
from ..schemas import (
    Availability,
    MovieShowsOut,
    ShowCinemaOut,
    ShowDateOut,
    ShowMovieOut,
    ShowOut,
)
from ..timeutil import ist_local, today_ist, utc_from_ist

router = APIRouter(prefix="/api/movies", tags=["shows"])

SHOW_WINDOW_DAYS = 7
FAST_FILLING_THRESHOLD = 0.7
LATE_NIGHT_CUTOFF = (23, 0)
EARLY_MORNING_CUTOFF = (9, 0)
SHOW_DATE_DETAIL = "date must be today or within the next 6 days"


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


def _availability(show: Show) -> Availability:
    if show.capacity and show.seats_sold / show.capacity >= FAST_FILLING_THRESHOLD:
        return Availability.FAST_FILLING
    return Availability.AVAILABLE


def _to_show_out(show: Show) -> ShowOut:
    local = ist_local(show.starts_at)
    clock = (local.hour, local.minute)
    return ShowOut(
        id=show.id,
        startsAt=local,
        format=show.format,
        language=show.language,
        availability=_availability(show),
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

    shows_by_cinema: dict[int, list[ShowOut]] = {}
    days_with_shows = set()
    for show, cinema_id in _city_movie_shows(movie_id, city, language, db):
        day = ist_local(show.starts_at).date()
        days_with_shows.add(day)
        if day != selected:
            continue
        shows_by_cinema.setdefault(cinema_id, []).append(_to_show_out(show))

    cinemas: list[ShowCinemaOut] = []
    if shows_by_cinema:
        for cinema in db.scalars(
            select(Cinema).where(Cinema.id.in_(list(shows_by_cinema))).order_by(Cinema.id)
        ):
            cinemas.append(_to_cinema_out(cinema, shows_by_cinema[cinema.id]))

    return MovieShowsOut(
        movie=ShowMovieOut(
            id=movie.id,
            title=movie.title,
            durationMinutes=movie.duration_minutes,
            certificate=movie.certificate,
            genres=movie.genres,
        ),
        dates=_date_strip(days_with_shows),
        cinemas=cinemas,
    )
