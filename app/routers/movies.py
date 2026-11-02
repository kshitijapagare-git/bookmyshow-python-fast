from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Cinema, City, Movie, Screen, Show
from ..schemas import MovieOut, MovieSummaryOut
from ..timeutil import today_ist, utc_from_ist

router = APIRouter(prefix="/api/movies", tags=["movies"])

UPCOMING_SHOW_DAYS = 7


def _to_summary(movie: Movie) -> MovieSummaryOut:
    return MovieSummaryOut(
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


def _to_detail(movie: Movie) -> MovieOut:
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


def _movie_ids_with_shows(city_slug: str, db: Session) -> set[int]:
    window_start = utc_from_ist(today_ist(), 0, 0)
    window_end = window_start + timedelta(days=UPCOMING_SHOW_DAYS)
    rows = db.scalars(
        select(Show.movie_id)
        .join(Screen, Show.screen_id == Screen.id)
        .join(Cinema, Screen.cinema_id == Cinema.id)
        .join(City, Cinema.city_id == City.id)
        .where(
            City.slug == city_slug,
            Show.starts_at >= window_start,
            Show.starts_at < window_end,
        )
        .distinct()
    )
    return set(rows)


@router.get("", response_model=list[MovieSummaryOut])
def list_movies(city: str | None = None, db: Session = Depends(get_db)):
    query = select(Movie).order_by(Movie.id)
    if city:
        query = query.where(Movie.id.in_(list(_movie_ids_with_shows(city, db))))
    movies = db.scalars(query)
    return [_to_summary(movie) for movie in movies]


@router.get("/{movie_id}", response_model=MovieOut)
def get_movie(movie_id: int, db: Session = Depends(get_db)):
    movie = db.get(Movie, movie_id)
    if movie is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Movie not found: {movie_id}",
        )
    return _to_detail(movie)
