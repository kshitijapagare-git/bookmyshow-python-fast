from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..media import (
    ALLOWED_CONTENT_TYPES,
    EmptyUploadError,
    MediaSettings,
    MediaTooLargeError,
    StorageBackend,
    get_media_settings,
    get_storage_backend,
)
from ..models import Cinema, City, MediaAsset, Movie, Screen, Show
from ..schemas import MediaAssetOut, MediaKind, MovieOut, MovieSummaryOut
from ..security import require_media_service_token
from ..timeutil import today_ist, utc_from_ist

router = APIRouter(prefix="/api/movies", tags=["movies"])

UPCOMING_SHOW_DAYS = 7
IMAGE_KINDS = (MediaKind.POSTER, MediaKind.BACKDROP)


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


def _max_bytes_for(kind: MediaKind, settings: MediaSettings) -> int:
    """The configured byte limit: the image one for posters and backdrops, else video."""
    if kind in IMAGE_KINDS:
        return settings.max_image_bytes
    return settings.max_video_bytes


@router.post(
    "/{movie_id}/media",
    response_model=MediaAssetOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_media_service_token)],
)
async def upload_movie_media(
    movie_id: int,
    kind: MediaKind = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    settings: MediaSettings = Depends(get_media_settings),
    storage: StorageBackend = Depends(get_storage_backend),
) -> MediaAssetOut:
    """Store one media file and make it the movie's current asset for its kind."""
    movie = db.get(Movie, movie_id)
    if movie is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Movie not found: {movie_id}",
        )

    declared = (file.content_type or "").split(";")[0].strip().lower()
    if declared not in ALLOWED_CONTENT_TYPES[kind]:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"{kind.value} does not accept {declared or 'that content type'}",
        )

    limit = _max_bytes_for(kind, settings)
    try:
        stored = await storage.save(
            kind=kind, content_type=declared, upload=file, max_bytes=limit
        )
    except MediaTooLargeError:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"{kind.value} upload exceeds the {limit} byte limit",
        ) from None
    except EmptyUploadError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Uploaded file is empty",
        ) from None

    url = storage.public_url(stored.key)
    # The previous asset of this kind keeps its row and its file; only the flag moves.
    for previous in db.scalars(
        select(MediaAsset).where(
            MediaAsset.movie_id == movie_id,
            MediaAsset.kind == kind.value,
            MediaAsset.is_current.is_(True),
        )
    ):
        previous.is_current = False

    asset = MediaAsset(
        movie_id=movie_id,
        kind=kind.value,
        storage_key=stored.key,
        url=url,
        content_type=declared,
        size_bytes=stored.size,
        sha256=stored.sha256,
        is_current=True,
    )
    db.add(asset)
    # The catalogue endpoints read the Movie row, so the current url is written there too.
    if kind is MediaKind.POSTER:
        movie.poster_url = url
    elif kind is MediaKind.BACKDROP:
        movie.backdrop_url = url
    else:
        movie.trailer_url = url
    db.commit()

    return MediaAssetOut(
        assetId=asset.id,
        kind=MediaKind(asset.kind),
        url=asset.url,
        size=asset.size_bytes,
        contentType=asset.content_type,
        sha256=asset.sha256,
    )
