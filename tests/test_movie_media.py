"""Uploading a movie's poster, backdrop and trailer through the internal media route.

Every upload lands in the MEDIA_UPLOAD_DIR that tests/conftest.py points at a fresh
temp directory, so the `/media/...` url the route returns can be fetched back and every
refusal can assert that no file and no MediaAsset row was created. The two byte limits
are shrunk through the get_media_settings override, so the 413 boundaries need only a
handful of bytes instead of a 5 MB image.
"""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.media import KIND_SUBDIRS, MediaSettings, get_media_settings
from app.models import MediaAsset
from app.schemas import MediaKind
from app.security import MEDIA_SERVICE_TOKEN

MEDIA_URL = "/api/movies/{movie_id}/media"
TOKEN_HEADER = "X-Internal-Token"
ASSET_KEYS = {"assetId", "kind", "url", "size", "contentType", "sha256"}
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
INVALID_TOKEN_DETAIL = "Invalid media service token"
MOVIE_ID = 1
POSTER_PAYLOAD = b"\x89PNG\r\n\x1a\nposter-bytes"
SECOND_POSTER_PAYLOAD = b"\x89PNG\r\n\x1a\nsecond-poster-bytes"
BACKDROP_PAYLOAD = b"\xff\xd8\xff\xe0backdrop-bytes"
TRAILER_PAYLOAD = b"\x00\x00\x00\x18ftypmp42trailer-bytes"


def _url(movie_id: int = MOVIE_ID) -> str:
    return MEDIA_URL.format(movie_id=movie_id)


@contextmanager
def _use_media(settings: MediaSettings) -> Iterator[MediaSettings]:
    """Serve the upload route these settings; the override is always popped again."""
    app.dependency_overrides[get_media_settings] = lambda: settings
    try:
        yield settings
    finally:
        app.dependency_overrides.pop(get_media_settings, None)


def _small_settings(
    *, max_image_bytes: int = 64, max_video_bytes: int = 128
) -> MediaSettings:
    """The app's own settings with tiny byte limits, so a 413 needs only a few bytes."""
    current = app.state.media_settings
    return MediaSettings(
        storage_backend=current.storage_backend,
        upload_dir=current.upload_dir,
        max_image_bytes=max_image_bytes,
        max_video_bytes=max_video_bytes,
        service_token=current.service_token,
    )


def _upload(
    client,
    movie_id: int,
    kind: str,
    payload: bytes,
    content_type: str,
    token: str | None = MEDIA_SERVICE_TOKEN,
) -> httpx.Response:
    """One multipart upload; the token header is left off entirely when it is None."""
    headers = {} if token is None else {TOKEN_HEADER: token}
    return client.post(
        _url(movie_id),
        files={"file": ("asset", payload, content_type)},
        data={"kind": kind},
        headers=headers,
    )


def _assets(movie_id: int | None = None, kind: str | None = None) -> list[MediaAsset]:
    """The MediaAsset rows, optionally narrowed to one movie and one kind."""
    with SessionLocal() as db:
        query = select(MediaAsset).order_by(MediaAsset.id)
        if movie_id is not None:
            query = query.where(MediaAsset.movie_id == movie_id)
        if kind is not None:
            query = query.where(MediaAsset.kind == kind)
        return list(db.scalars(query))


def _stored_files() -> set[Path]:
    """Every file under the upload directory, .part leftovers included."""
    root = Path(app.state.media_settings.upload_dir)
    return {path for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize(
    ("kind", "content_type", "payload"),
    [
        ("POSTER", "image/png", POSTER_PAYLOAD),
        ("BACKDROP", "image/jpeg", BACKDROP_PAYLOAD),
        ("TRAILER", "video/mp4", TRAILER_PAYLOAD),
    ],
)
def test_upload_each_kind_returns_the_asset_metadata(
    client, kind: str, content_type: str, payload: bytes
) -> None:
    response = _upload(client, MOVIE_ID, kind, payload, content_type)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == ASSET_KEYS
    assert body["kind"] == kind
    assert body["contentType"] == content_type
    assert body["size"] == len(payload)
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()
    assert body["url"].startswith(f"/media/{KIND_SUBDIRS[MediaKind(kind)]}/")

    # The url the route returned really serves the bytes it stored.
    fetched = client.get(body["url"])
    assert fetched.status_code == 200
    assert fetched.content == payload

    rows = _assets(MOVIE_ID, kind)
    assert len(rows) == 1
    asset = rows[0]
    assert asset.id == body["assetId"]
    assert asset.is_current is True
    assert asset.url == body["url"]
    assert asset.content_type == content_type
    assert asset.size_bytes == len(payload)
    assert asset.sha256 == body["sha256"]
    assert (
        Path(app.state.media_settings.upload_dir) / asset.storage_key
    ).read_bytes() == payload


def test_upload_writes_the_current_url_into_the_movie(client) -> None:
    poster = _upload(client, MOVIE_ID, "POSTER", POSTER_PAYLOAD, "image/png")
    backdrop = _upload(client, MOVIE_ID, "BACKDROP", BACKDROP_PAYLOAD, "image/jpeg")
    trailer = _upload(client, MOVIE_ID, "TRAILER", TRAILER_PAYLOAD, "video/mp4")
    for response in (poster, backdrop, trailer):
        assert response.status_code == 201

    detail = client.get(f"/api/movies/{MOVIE_ID}")

    assert detail.status_code == 200
    body = detail.json()
    assert body["posterUrl"] == poster.json()["url"]
    assert body["backdropUrl"] == backdrop.json()["url"]
    assert body["trailerUrl"] == trailer.json()["url"]

    listed = client.get("/api/movies")

    assert listed.status_code == 200
    by_id = {movie["id"]: movie for movie in listed.json()}
    assert by_id[MOVIE_ID]["posterUrl"] == poster.json()["url"]


def test_a_second_poster_is_current_and_the_first_is_kept(client) -> None:
    first = _upload(client, MOVIE_ID, "POSTER", POSTER_PAYLOAD, "image/png")
    second = _upload(client, MOVIE_ID, "POSTER", SECOND_POSTER_PAYLOAD, "image/png")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["url"] != second.json()["url"]

    rows = _assets(MOVIE_ID, "POSTER")
    assert [row.id for row in rows] == [
        first.json()["assetId"],
        second.json()["assetId"],
    ]
    assert [row.is_current for row in rows] == [False, True]

    # The superseded asset keeps its file, so both urls still serve their own bytes.
    assert client.get(first.json()["url"]).content == POSTER_PAYLOAD
    assert client.get(second.json()["url"]).content == SECOND_POSTER_PAYLOAD

    detail = client.get(f"/api/movies/{MOVIE_ID}")

    assert detail.status_code == 200
    assert detail.json()["posterUrl"] == second.json()["url"]


@pytest.mark.parametrize(
    ("kind", "content_type"),
    [
        ("POSTER", "image/gif"),
        ("BACKDROP", "video/mp4"),
        ("TRAILER", "image/png"),
    ],
)
def test_an_unsupported_content_type_is_415(client, kind: str, content_type: str) -> None:
    before = _stored_files()

    response = _upload(client, MOVIE_ID, kind, POSTER_PAYLOAD, content_type)

    assert response.status_code == 415
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Unsupported Media Type"
    assert body["status"] == 415
    assert body["instance"] == _url()
    assert _assets() == []
    assert _stored_files() == before


def test_the_image_limit_comes_from_the_settings(client) -> None:
    with _use_media(_small_settings(max_image_bytes=8, max_video_bytes=16)):
        at_the_limit = _upload(client, MOVIE_ID, "POSTER", b"12345678", "image/png")
        over_the_limit = _upload(client, MOVIE_ID, "POSTER", b"123456789", "image/png")

    assert at_the_limit.status_code == 201
    assert at_the_limit.json()["size"] == 8

    assert over_the_limit.status_code == 413
    body = over_the_limit.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["status"] == 413
    assert body["instance"] == _url()

    rows = _assets(MOVIE_ID, "POSTER")
    assert [row.id for row in rows] == [at_the_limit.json()["assetId"]]
    rejected_name = hashlib.sha256(b"123456789").hexdigest()
    assert not any(
        path.name.startswith(rejected_name) for path in _stored_files()
    )
    assert not list(Path(app.state.media_settings.upload_dir).rglob("*.part"))


def test_the_video_limit_comes_from_the_settings(client) -> None:
    with _use_media(_small_settings(max_image_bytes=8, max_video_bytes=10)):
        at_the_limit = _upload(client, MOVIE_ID, "TRAILER", b"0123456789", "video/mp4")
        over_the_limit = _upload(
            client, MOVIE_ID, "TRAILER", b"0123456789A", "video/mp4"
        )

    assert at_the_limit.status_code == 201
    assert at_the_limit.json()["size"] == 10

    assert over_the_limit.status_code == 413
    body = over_the_limit.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["status"] == 413
    assert body["instance"] == _url()

    rows = _assets(MOVIE_ID, "TRAILER")
    assert [row.id for row in rows] == [at_the_limit.json()["assetId"]]
    assert rows[0].size_bytes == 10


def test_an_empty_upload_is_422(client) -> None:
    before = _stored_files()

    response = _upload(client, MOVIE_ID, "POSTER", b"", "image/png")

    assert response.status_code == 422
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["status"] == 422
    assert body["instance"] == _url()
    assert _assets() == []
    assert _stored_files() == before


def test_an_unknown_movie_is_404(client) -> None:
    response = _upload(client, 999999, "POSTER", POSTER_PAYLOAD, "image/png")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == "Movie not found: 999999"
    assert body["instance"] == _url(999999)
    assert _assets() == []


@pytest.mark.parametrize("token", [None, "not-the-token"])
def test_the_internal_token_is_required(client, token: str | None) -> None:
    before = _stored_files()

    response = _upload(client, MOVIE_ID, "POSTER", POSTER_PAYLOAD, "image/png", token)

    assert response.status_code == 401
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Unauthorized"
    assert body["status"] == 401
    assert body["detail"] == INVALID_TOKEN_DETAIL
    assert body["instance"] == _url()
    assert _assets() == []
    assert _stored_files() == before
