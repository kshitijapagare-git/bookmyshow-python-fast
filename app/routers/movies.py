from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Cinema, City, Movie, Screen, Show
from ..schemas import (
    CinemaShowsOut,
    MovieDetailOut,
    MovieOut,
    MovieShowsOut,
    NonBlank,
    ShowMovieOut,
    ShowOut,
    ShowsDateOut,
)
from ..showtime import (
    availability_for,
    is_early_morning,
    is_late_night,
    local_day_start,
    local_start,
    show_days,
    shift_date,
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


@router.get("/{movie_id}/shows", response_model=MovieShowsOut)
def get_movie_shows(
    movie_id: int,
    city: NonBlank,
    date: date,
    language: NonBlank | None = None,
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

    shows = list(db.scalars(_shows_query(db, movie_id, city, language)))

    days_with_shows = {local_start(show.starts_at).date() for show in shows}
    dates = [
        ShowsDateOut(date=day, hasShows=day in days_with_shows) for day in days
    ]

    day_start = local_day_start(date)
    day_end = local_day_start(date + timedelta(days=1))
    grouped: dict[int, list[Show]] = {}
    for show in shows:
        if day_start <= show.starts_at < day_end:
            grouped.setdefault(show.screen.cinema_id, []).append(show)

    cinemas = db.scalars(
        select(Cinema).where(Cinema.id.in_(list(grouped))).order_by(Cinema.id)
    )
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
    )
