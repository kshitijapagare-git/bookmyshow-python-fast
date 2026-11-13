"""Direct-to-S3 uploads end to end: presign, complete, the media routes and the proxy.

With `MEDIA_STORAGE_BACKEND=s3` the browser PUTs the bytes straight into the bucket, so
these tests stand in for it by writing the payload under the key the presigned url names.
`FakeS3Client` is the same in-memory client tests/test_media_s3_backend.py drives, and
`build_s3_client` is patched to answer it, so nothing here contacts a bucket, needs
credentials or reads a real object.

`create_app` decides once, when it is built, whether `/media/{key}` is the media router's
streaming proxy or a static mount of MEDIA_UPLOAD_DIR, so the S3 app is constructed here
with the S3 environment already in place; the local backend stays the suite's default.
The refusals and the local fallback run against the shared app through the very
`get_storage_backend` the routes depend on, so no test assigns to `app.state`.

The real-endpoint round trip at the bottom is opt-in on `S3_ENDPOINT_URL` (LocalStack or
any S3-compatible server), so a CI run without a bucket stays green.
"""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.media_s3 as media_s3
from app.db import SessionLocal
from app.main import app, create_app
from app.media import (
    LocalStorageBackend,
    MediaSettings,
    StorageBackend,
    build_storage_backend,
    get_media_settings,
    get_storage_backend,
)
from app.media_s3 import S3StorageBackend, normalized_prefix
from app.models import MediaAsset, User
from app.routers.media import COMPLETE_NOT_UPLOADED_DETAIL, PRESIGN_INVALID_DETAIL
from app.schemas import MediaKind
from app.security import MEDIA_SERVICE_TOKEN, issue_token
from test_media_s3_backend import FakeS3Client

PRESIGN_URL = "/api/media/presign"
COMPLETE_URL = "/api/media/{assetId}/complete"
MOVIE_MEDIA_URL = "/api/movies/{movieId}/media"
REVIEW_PHOTO_URL = "/api/movies/{movieId}/reviews/{reviewId}/photo"
TOKEN_HEADER = "X-Internal-Token"
BUCKET = "bms-media"
REGION = "ap-south-1"
PREFIX = "media"
MOVIE_ID = 1
REVIEW_ID = 501
S3_ENDPOINT_ENV = "S3_ENDPOINT_URL"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
PRESIGN_KEYS = {"assetId", "uploadUrl", "method", "expiresIn", "headers"}
COMPLETE_KEYS = {
    "assetId",
    "url",
    "contentType",
    "size",
    "sha256",
    "kind",
    "verified",
}
AVATAR_PAYLOAD = b"\x89PNG\r\n\x1a\navatar-bytes"
POSTER_PAYLOAD = b"\x89PNG\r\n\x1a\nposter-bytes"
REVIEW_PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\nreview-photo-bytes"


def _create_user(phone: str) -> tuple[int, str]:
    """Insert a signed-in moviegoer and mint their JWT, as the other media tests do."""
    with SessionLocal() as db:
        user = User(phone=phone, name=None, created_at=datetime.now(timezone.utc))
        db.add(user)
        db.commit()
        return user.id, issue_token(user_id=user.id)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _avatar_body(**overrides: object) -> dict[str, object]:
    """The declaration a moviegoer makes for their profile photo."""
    body: dict[str, object] = {
        "purpose": "avatar",
        "filename": "me.png",
        "contentType": "image/png",
        "size": len(AVATAR_PAYLOAD),
    }
    body.update(overrides)
    return body


def _asset(asset_id: int) -> MediaAsset | None:
    """The row itself, so an assertion can read the persisted flags and the reserved key."""
    with SessionLocal() as db:
        return db.get(MediaAsset, asset_id)


def _key_of(upload_url: str) -> str:
    """The object key a presigned url names: the browser's PUT writes exactly there."""
    path = upload_url.partition("?")[0]
    prefix = f"https://{BUCKET}.s3.amazonaws.com/"
    assert path.startswith(prefix)
    return path[len(prefix) :]


def _put_to_s3(fake: FakeS3Client, upload_url: str, payload: bytes) -> str:
    """Stand in for the browser: the bytes land under the key the presigned url names."""
    key = _key_of(upload_url)
    fake.objects[key] = payload
    return key


def _s3_settings(base: MediaSettings) -> MediaSettings:
    """The app's own settings with every S3 knob the ticket names filled in."""
    return replace(
        base,
        storage_backend="s3",
        s3_bucket=BUCKET,
        s3_region=REGION,
        s3_endpoint_url="",
        s3_media_prefix=PREFIX,
        s3_sse="AES256",
    )


@contextmanager
def _use_settings(settings: MediaSettings) -> Iterator[StorageBackend]:
    """Serve these settings and the backend they select; both overrides are popped."""
    backend = build_storage_backend(settings)
    app.dependency_overrides[get_media_settings] = lambda: settings
    app.dependency_overrides[get_storage_backend] = lambda: backend
    try:
        yield backend
    finally:
        app.dependency_overrides.pop(get_media_settings, None)
        app.dependency_overrides.pop(get_storage_backend, None)


@pytest.fixture()
def s3_client(monkeypatch) -> FakeS3Client:
    """Every S3 client the app builds is this in-memory fake, whatever the settings."""
    fake = FakeS3Client()
    monkeypatch.setattr(media_s3, "build_s3_client", lambda settings: fake)
    return fake


@pytest.fixture()
def s3_settings(s3_client: FakeS3Client) -> Iterator[MediaSettings]:
    """The shared app's settings with the S3 ones replaced, installed only as overrides."""
    settings = _s3_settings(app.state.media_settings)
    with _use_settings(settings) as backend:
        assert isinstance(backend, S3StorageBackend)
        yield settings


@pytest.fixture()
def s3_app_client(s3_client: FakeS3Client, monkeypatch) -> Iterator[TestClient]:
    """An app wired for S3, so `/media/{key}` is the streaming proxy, not a disk mount.

    The environment is read once inside `create_app`, so it has to be in place before
    the app is built; `build_s3_client` is already patched, so no bucket is contacted.
    """
    monkeypatch.setenv("MEDIA_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    monkeypatch.setenv("S3_REGION", REGION)
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_MEDIA_PREFIX", PREFIX)
    monkeypatch.setenv("S3_SSE", "AES256")

    application = create_app()
    with TestClient(application) as test_client:
        yield test_client


def test_direct_upload_lands_in_s3_and_is_served_through_the_proxy(
    s3_app_client: TestClient, s3_client: FakeS3Client
) -> None:
    user_id, token = _create_user("+919000006101")

    presigned = s3_app_client.post(
        PRESIGN_URL, json=_avatar_body(), headers=_auth(token)
    )

    assert presigned.status_code == 200
    body = presigned.json()
    assert set(body) == PRESIGN_KEYS
    assert body["method"] == "PUT"
    assert body["headers"] == {"Content-Type": "image/png"}
    upload_url = body["uploadUrl"]
    assert upload_url.startswith(f"https://{BUCKET}.s3.amazonaws.com/{PREFIX}/avatars/")
    assert "x-amz-server-side-encryption=AES256" in upload_url
    # Direct-to-S3 means the browser never posts to our own host.
    assert not upload_url.startswith("/api/media/")

    pending = _asset(body["assetId"])
    assert pending is not None
    assert pending.user_id == user_id
    assert pending.kind == MediaKind.AVATAR.value
    # The object key is reserved at presign; the bytes and the url are not there yet.
    assert pending.storage_key is not None
    assert pending.storage_key.startswith(f"{PREFIX}/avatars/")
    assert pending.storage_key == _key_of(upload_url)
    assert pending.url is None
    assert pending.sha256 is None
    assert pending.verified is False
    assert pending.is_current is False

    key = _put_to_s3(s3_client, upload_url, AVATAR_PAYLOAD)
    assert s3_client.objects[key] == AVATAR_PAYLOAD

    completed = s3_app_client.post(
        COMPLETE_URL.format(assetId=body["assetId"]), headers=_auth(token)
    )

    assert completed.status_code == 200
    complete_body = completed.json()
    assert set(complete_body) == COMPLETE_KEYS
    assert complete_body == {
        "assetId": body["assetId"],
        "url": f"/media/{key}",
        "contentType": "image/png",
        "size": len(AVATAR_PAYLOAD),
        "sha256": hashlib.sha256(AVATAR_PAYLOAD).hexdigest(),
        "kind": "AVATAR",
        "verified": True,
    }

    # Completing reads the object back: a HEAD plus one full streamed GET.
    assert {"Bucket": BUCKET, "Key": key} in s3_client.head_calls
    assert {"Bucket": BUCKET, "Key": key} in s3_client.get_calls

    current = _asset(body["assetId"])
    assert current is not None
    assert current.verified is True
    assert current.is_current is True
    assert current.sha256 == complete_body["sha256"]

    # `url` is still the app's own `/media/...` path, streamed from the bucket.
    served = s3_app_client.get(complete_body["url"])

    assert served.status_code == 200
    assert served.content == AVATAR_PAYLOAD
    assert served.headers["content-type"] == "image/png"


def test_complete_before_the_object_lands_is_422(
    client, s3_settings: MediaSettings, s3_client: FakeS3Client
) -> None:
    _, token = _create_user("+919000006102")
    presigned = client.post(PRESIGN_URL, json=_avatar_body(), headers=_auth(token))
    assert presigned.status_code == 200
    asset_id = presigned.json()["assetId"]
    url = COMPLETE_URL.format(assetId=asset_id)

    response = client.post(url, headers=_auth(token))

    assert response.status_code == 422
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["status"] == 422
    assert body["detail"] == COMPLETE_NOT_UPLOADED_DETAIL
    assert body["instance"] == url

    # Nothing was ever PUT, so the object store is untouched.
    assert s3_client.objects == {}

    pending = _asset(asset_id)
    assert pending is not None
    assert pending.url is None
    assert pending.sha256 is None
    assert pending.verified is False
    assert pending.is_current is False


def test_complete_with_bytes_of_another_size_is_422(
    client, s3_settings: MediaSettings, s3_client: FakeS3Client
) -> None:
    _, token = _create_user("+919000006103")
    presigned = client.post(PRESIGN_URL, json=_avatar_body(), headers=_auth(token))
    assert presigned.status_code == 200
    asset_id = presigned.json()["assetId"]
    _put_to_s3(s3_client, presigned.json()["uploadUrl"], AVATAR_PAYLOAD + b"extra")
    url = COMPLETE_URL.format(assetId=asset_id)

    response = client.post(url, headers=_auth(token))

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == 422
    assert body["detail"] == PRESIGN_INVALID_DETAIL
    assert body["instance"] == url

    pending = _asset(asset_id)
    assert pending is not None
    assert pending.verified is False
    assert pending.is_current is False


def test_movie_and_review_media_are_stored_in_s3_and_served_back(
    s3_app_client: TestClient, s3_client: FakeS3Client
) -> None:
    _, token = _create_user("+919000006104")

    poster = s3_app_client.post(
        MOVIE_MEDIA_URL.format(movieId=MOVIE_ID),
        files={"file": ("asset", POSTER_PAYLOAD, "image/png")},
        data={"kind": "POSTER"},
        headers={TOKEN_HEADER: MEDIA_SERVICE_TOKEN},
    )

    assert poster.status_code == 201
    poster_body = poster.json()
    assert poster_body["url"].startswith(f"/media/{PREFIX}/posters/")
    assert poster_body["sha256"] == hashlib.sha256(POSTER_PAYLOAD).hexdigest()
    poster_key = poster_body["url"].removeprefix("/media/")
    assert s3_client.objects[poster_key] == POSTER_PAYLOAD

    served_poster = s3_app_client.get(poster_body["url"])

    assert served_poster.status_code == 200
    assert served_poster.content == POSTER_PAYLOAD

    photo = s3_app_client.post(
        REVIEW_PHOTO_URL.format(movieId=MOVIE_ID, reviewId=REVIEW_ID),
        files={"file": ("photo", REVIEW_PHOTO_PAYLOAD, "image/png")},
        headers=_auth(token),
    )

    assert photo.status_code == 201
    photo_body = photo.json()
    assert photo_body["url"].startswith(f"/media/{PREFIX}/review-photos/")
    assert photo_body["verified"] is True
    photo_key = photo_body["url"].removeprefix("/media/")
    assert s3_client.objects[photo_key] == REVIEW_PHOTO_PAYLOAD

    served_photo = s3_app_client.get(photo_body["url"])

    assert served_photo.status_code == 200
    assert served_photo.content == REVIEW_PHOTO_PAYLOAD


def test_s3_without_a_bucket_falls_back_to_the_local_backend(client) -> None:
    settings = replace(
        app.state.media_settings,
        storage_backend="s3",
        s3_bucket="",
        s3_region=REGION,
        s3_media_prefix=PREFIX,
    )

    assert isinstance(build_storage_backend(settings), LocalStorageBackend)

    _, token = _create_user("+919000006105")
    with _use_settings(settings) as backend:
        assert isinstance(backend, LocalStorageBackend)
        response = client.post(
            PRESIGN_URL, json=_avatar_body(), headers=_auth(token)
        )

    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "PUT"
    # The unusable S3 configuration selected the local backend, so its own url is used.
    assert body["uploadUrl"].startswith(
        f"/api/media/{body['assetId']}/upload?token="
    )


async def _one_chunk(payload: bytes) -> AsyncIterator[bytes]:
    """One chunk, so `save_stream` sees exactly this payload."""
    yield payload


@pytest.mark.skipif(
    not os.getenv(S3_ENDPOINT_ENV),
    reason=f"set {S3_ENDPOINT_ENV} (e.g. to LocalStack) to run the real-bucket round trip",
)
def test_a_real_s3_endpoint_round_trip() -> None:
    """Put, read, hash, presign and delete against whatever `S3_ENDPOINT_URL` names."""
    settings = MediaSettings.from_env()
    backend = S3StorageBackend(settings)
    prefix = normalized_prefix(settings.s3_media_prefix)

    async def _round_trip() -> None:
        stored = await backend.save_stream(
            kind=MediaKind.AVATAR,
            content_type="image/png",
            chunks=_one_chunk(AVATAR_PAYLOAD),
            max_bytes=len(AVATAR_PAYLOAD),
        )
        assert stored.key.startswith(f"{prefix}/avatars/")
        assert stored.sha256 == hashlib.sha256(AVATAR_PAYLOAD).hexdigest()
        assert stored.size == len(AVATAR_PAYLOAD)

        assert await backend.exists(key=stored.key) is True

        digest = await backend.digest(key=stored.key, algorithm="sha256")
        assert digest is not None
        assert digest.sha256 == stored.sha256
        assert digest.size == stored.size

        streamed = b"".join(
            [chunk async for chunk in backend.iter_chunks(key=stored.key)]
        )
        assert streamed == AVATAR_PAYLOAD

        presigned = backend.presigned_upload_url(
            asset_id=1,
            key=stored.key,
            content_type="image/png",
            expires_seconds=60,
        )
        assert presigned.method == "PUT"
        assert presigned.url.startswith("http")

        await backend.delete(key=stored.key)
        assert await backend.exists(key=stored.key) is False

    asyncio.run(_round_trip())
