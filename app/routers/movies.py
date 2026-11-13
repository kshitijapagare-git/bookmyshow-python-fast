from datetime import timedelta
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
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
    max_bytes_for,
)
from ..models import Cinema, City, MediaAsset, Movie, Screen, Show
from ..schemas import MediaAssetOut, MediaKind, MovieOut, MovieSummaryOut
from ..security import require_media_service_token
from ..streaming import (
    UnsatisfiableRangeError,
    iter_file_range,
    parse_range_header,
)
from ..timeutil import today_ist, utc_from_ist

router = APIRouter(prefix="/api/movies", tags=["movies"])

UPCOMING_SHOW_DAYS = 7
ACCEPT_RANGES = "bytes"
RANGE_NOT_SATISFIABLE_DETAIL = "Range not satisfiable"


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


def _to_detail(movie: Movie, db: Session) -> MovieOut:
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
        trailerUrl=_current_trailer_url(movie, db),
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
    return _to_detail(movie, db)


def _max_bytes_for(kind: MediaKind, settings: MediaSettings) -> int:
    """The configured byte limit, as the shared image/video rule in app.media decides."""
    return max_bytes_for(kind, settings)


def trailer_endpoint_url(movie_id: int) -> str:
    """The public url of the streaming route: where the detail's trailer pill points."""
    return f"/api/movies/{movie_id}/trailer"


def _current_trailer_url(movie: Movie, db: Session) -> str | None:
    """The trailer pill's url: the streaming endpoint once a trailer upload exists.

    A movie with no current TRAILER row keeps its stored `trailer_url` untouched, so a
    seeded movie's YouTube link (which is not a streamable file) still reads as before.
    """
    current = db.scalar(
        select(MediaAsset.id).where(
            MediaAsset.movie_id == movie.id,
            MediaAsset.kind == MediaKind.TRAILER.value,
            MediaAsset.is_current.is_(True),
        )
    )
    if current is None:
        return movie.trailer_url
    return trailer_endpoint_url(movie.id)


def _current_trailer_asset(movie_id: int, db: Session) -> MediaAsset:
    """The movie's current TRAILER row.

    The row, not `movie.trailer_url`, is the source of truth: a seeded movie's
    trailer_url holds a YouTube link that is not a file, and only an upload creates
    a row whose storage key points at streamable bytes.
    """
    asset = db.scalars(
        select(MediaAsset)
        .where(
            MediaAsset.movie_id == movie_id,
            MediaAsset.kind == MediaKind.TRAILER.value,
            MediaAsset.is_current.is_(True),
        )
        .order_by(MediaAsset.id.desc())
        .limit(1)
    ).first()
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trailer not found: {movie_id}",
        )
    return asset


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


@router.get(
    "/{movie_id}/trailer",
    response_class=StreamingResponse,
    responses={
        206: {"description": "The requested byte range of the trailer"},
        404: {"description": "No such movie, or it has no current trailer"},
        416: {"description": "The Range header names no slice of the trailer"},
    },
)
async def stream_movie_trailer(
    movie_id: int,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_backend),
) -> StreamingResponse:
    """Stream the movie's current trailer, honouring one `Range: bytes` slice.

    The file is read through `iter_file_range`, so a browser can start playing after
    the first chunk and seek with a Range request instead of downloading the whole
    video. The route is public: a moviegoer's player loads it directly.
    """
    if db.get(Movie, movie_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Movie not found: {movie_id}",
        )
    asset = _current_trailer_asset(movie_id, db)
    path = storage.path_for(asset.storage_key)

    try:
        byte_range = parse_range_header(range_header, asset.size_bytes)
    except UnsatisfiableRangeError:
        # A malformed or multi-part header is the same 416 as an unsatisfiable one,
        # and the shared handler turns this into the RFC 7807 body.
        raise HTTPException(
            status_code=416, detail=RANGE_NOT_SATISFIABLE_DETAIL
        ) from None

    headers = {"Accept-Ranges": ACCEPT_RANGES}
    if byte_range is None:
        headers["Content-Length"] = str(asset.size_bytes)
        return StreamingResponse(
            iter_file_range(path, None),
            media_type=asset.content_type,
            headers=headers,
        )

    headers["Content-Range"] = (
        f"bytes {byte_range.start}-{byte_range.end}/{asset.size_bytes}"
    )
    headers["Content-Length"] = str(byte_range.length)
    return StreamingResponse(
        iter_file_range(path, byte_range),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=asset.content_type,
        headers=headers,
    )
