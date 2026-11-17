"""The whole media pipeline end to end against a MinIO-backed storage seam.

`MEDIA_STORAGE_BACKEND=minio` selects `MinioStorageBackend`, and this module proves the
app's own routes work through it: a poster, a backdrop and a trailer uploaded by the
internal media route, an avatar uploaded straight into the bucket through a presigned PUT
and then completed, a review photo, and the object the download route streams back -- each
of them arriving at the MinIO bucket rather than on disk.

Nothing here needs boto3, a network or a running MinIO: a hand-written `FakeMinioClient`
(the same shape tests/test_minio_storage.py uses -- recording every put, get, head,
delete, bucket probe and presigned call) is injected through
`MinioStorageBackend(client=...)`, and the backend is handed to the routes through
`app.dependency_overrides[get_storage_backend]`. Every override is popped in a `finally`,
so a later test reading `GET /api/media/health/storage` sees the app's own backend again
and no test depends on execution order.

Two acceptance criteria pin behaviour the backend owns rather than a route, and are
proved the way the seams actually work here. The presigned PUT a client receives is
MinIO's own signed url, not this API's `/upload` route, so the browser's upload is
recorded by putting into the fake bucket directly -- exactly as tests/test_s3_storage.py
drives its completion route. And no media route deletes an asset (a superseded one keeps
its row and its file, which tests/test_movie_media.py asserts), so deletion is exercised
through the same storage seam those routes read through. The review-photo author check
belongs to the presign route, because the multipart photo route carries only a bearer
token and never resolves the review.
"""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.media import (
    MediaSettings,
    build_storage_backend,
    get_storage_backend,
)
from app.minio_storage import MinioStorageBackend
from app.models import MediaAsset, User
from app.reviews_client import get_reviews_client
from app.s3_storage import S3_PRIVATE_ACL, S3_READ_CHUNK_SIZE
from app.security import MEDIA_SERVICE_TOKEN, issue_token

MINIO_ENDPOINT = "localhost:9000"
# The endpoint as a url: a bare `host:port` with MINIO_SECURE off gains http.
MINIO_ENDPOINT_URL = f"http://{MINIO_ENDPOINT}"
MINIO_BUCKET = "bms-minio-media"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"

HEALTH_URL = "/api/media/health/storage"
PRESIGN_URL = "/api/media/presign"
COMPLETE_URL = "/api/media/{asset_id}/complete"
DOWNLOAD_URL = "/api/media/{asset_id}"
AVATAR_URL = "/api/users/me/avatar"
MOVIE_MEDIA_URL = "/api/movies/{movie_id}/media"
REVIEW_PHOTO_URL = "/api/movies/{movie_id}/reviews/{review_id}/photo"

HEALTH_KEYS = {"backend", "reachable", "bucket", "endpoint"}
PRESIGN_EXPIRES_SECONDS = 900
MOVIE_ID = 1
REVIEW_ID = 501
CONTENT_TYPE = "image/png"
POSTER_PAYLOAD = b"\x89PNG\r\n\x1a\nposter-bytes"
BACKDROP_PAYLOAD = b"\xff\xd8\xff\xe0backdrop-bytes"
TRAILER_PAYLOAD = b"\x00\x00\x00\x18ftypmp42trailer-bytes"
AVATAR_PAYLOAD = b"\x89PNG\r\n\x1a\navatar-bytes"
PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\nreview-photo-bytes"


class _ClientError(Exception):
    """A stand-in for botocore's ClientError, carrying the same `response` shape."""

    def __init__(self, code: str = "404", status_code: int = 404) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        }


class _FakeBody:
    """The `StreamingBody` shape `get_object` answers with."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.closed = False

    def iter_chunks(self, chunk_size: int = S3_READ_CHUNK_SIZE) -> Iterator[bytes]:
        for start in range(0, len(self._payload), chunk_size):
            yield self._payload[start : start + chunk_size]

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        self.closed = True


def _range_slice(header: str, payload: bytes) -> bytes:
    """The inclusive slice an S3 `Range: bytes=start-end` header asks for."""
    unit, _, specifier = header.partition("=")
    assert unit == "bytes"
    start_text, _, end_text = specifier.partition("-")
    start = int(start_text)
    end = int(end_text) if end_text else len(payload) - 1
    return payload[start : end + 1]


class FakeMinioClient:
    """A hand-written stand-in for boto3's S3 client pointed at MinIO.

    Every argument a real call would carry is recorded with its `Bucket` and `Key`, so a
    test can assert the wire key (prefix included), the private ACL, the stored bytes and
    the presigned expiry without a bucket or a network. A missing object raises the same
    `response`-shaped 404 botocore raises, which is what the backend's not-found handling
    reads. `generate_presigned_url` answers at the configured MinIO endpoint, so a signed
    PUT or GET the routes hand out names MinIO itself rather than a cloud host.
    """

    def __init__(
        self,
        *,
        buckets: tuple[str, ...] = (MINIO_BUCKET,),
        head_bucket_error: BaseException | None = None,
    ) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.buckets: set[str] = set(buckets)
        self.head_bucket_error = head_bucket_error
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.head_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.head_bucket_calls: list[dict[str, Any]] = []
        self.presigned_calls: list[dict[str, Any]] = []

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: Any,
        ContentType: str | None = None,
        ACL: str | None = None,
        ServerSideEncryption: str | None = None,
    ) -> dict[str, Any]:
        payload = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.put_calls.append(
            {
                "Bucket": Bucket,
                "Key": Key,
                "Body": payload,
                "ContentType": ContentType,
                "ACL": ACL,
                "ServerSideEncryption": ServerSideEncryption,
            }
        )
        self.objects[Key] = (payload, ContentType or "")
        return {"ETag": '"fake-etag"'}

    def get_object(
        self, *, Bucket: str, Key: str, Range: str | None = None
    ) -> dict[str, Any]:
        if Key not in self.objects:
            raise _ClientError("NoSuchKey")
        payload, content_type = self.objects[Key]
        self.get_calls.append({"Bucket": Bucket, "Key": Key, "Range": Range})
        if Range is not None:
            payload = _range_slice(Range, payload)
        return {
            "Body": _FakeBody(payload),
            "ContentLength": len(payload),
            "ContentType": content_type,
        }

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise _ClientError("404")
        payload, content_type = self.objects[Key]
        self.head_calls.append({"Bucket": Bucket, "Key": Key})
        return {"ContentLength": len(payload), "ContentType": content_type}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self.delete_calls.append({"Bucket": Bucket, "Key": Key})
        if Key not in self.objects:
            raise _ClientError("NoSuchKey")
        del self.objects[Key]
        return {}

    def head_bucket(self, *, Bucket: str) -> dict[str, Any]:
        self.head_bucket_calls.append({"Bucket": Bucket})
        if self.head_bucket_error is not None:
            raise self.head_bucket_error
        if Bucket not in self.buckets:
            raise _ClientError("404", 404)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: dict[str, Any] | None = None,
        ExpiresIn: int = 3600,
        HttpMethod: str | None = None,
    ) -> str:
        params = dict(Params or {})
        self.presigned_calls.append(
            {"method": ClientMethod, "params": params, "expires_in": ExpiresIn}
        )
        bucket = params.get("Bucket", "")
        key = params.get("Key", "")
        return (
            f"{MINIO_ENDPOINT_URL}/{bucket}/{key}"
            f"?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=fake"
        )


def minio_settings_for(base: MediaSettings, **overrides: object) -> MediaSettings:
    """This app's own settings with the seven `MINIO_*` knobs pointed at a local MinIO."""
    values: dict[str, object] = {
        "minio_bucket": MINIO_BUCKET,
        "minio_endpoint": MINIO_ENDPOINT,
        "minio_access_key": MINIO_ACCESS_KEY,
        "minio_secret_key": MINIO_SECRET_KEY,
        "minio_secure": False,
        "minio_media_prefix": "",
        "minio_create_bucket": False,
    }
    values.update(overrides)
    return replace(base, **values)  # type: ignore[arg-type]


def _backend(
    fake: FakeMinioClient | None = None, **overrides: object
) -> tuple[MinioStorageBackend, FakeMinioClient]:
    """A MinIO backend over the fake client, based on this app's own settings."""
    client = fake if fake is not None else FakeMinioClient()
    settings = minio_settings_for(app.state.media_settings, **overrides)
    return MinioStorageBackend(settings, client=client), client


@contextmanager
def use_minio_storage(
    storage: MinioStorageBackend,
) -> Iterator[MinioStorageBackend]:
    """Serve this MinIO backend to the media routes; the override is always popped."""
    app.dependency_overrides[get_storage_backend] = lambda: storage
    try:
        yield storage
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


class _StubReviews:
    """The one review read the presign ownership check makes, from a fixed mapping."""

    def __init__(self, reviews: dict[int, dict[str, object]]) -> None:
        self.reviews = reviews
        self.lookups: list[int] = []

    def get_review(self, *, review_id: int) -> dict[str, object] | None:
        self.lookups.append(review_id)
        return self.reviews.get(review_id)


@contextmanager
def _use_reviews(
    reviews: dict[int, dict[str, object]]
) -> Iterator[_StubReviews]:
    """Answer the presign route's reviews dependency; the override is always popped."""
    stub = _StubReviews(reviews)
    app.dependency_overrides[get_reviews_client] = lambda: stub
    try:
        yield stub
    finally:
        app.dependency_overrides.pop(get_reviews_client, None)


def _review(review_id: int, user_id: int, movie_id: int = MOVIE_ID) -> dict[str, object]:
    """One review, in the shape the ratings service answers a single read with."""
    return {
        "id": review_id,
        "movieId": movie_id,
        "userId": user_id,
        "rating": 5,
        "comment": "Loved it",
        "verified": True,
        "status": "ACTIVE",
        "createdAt": "2026-10-20T10:00:00+05:30",
    }


def _user(phone: str) -> tuple[int, str]:
    """Insert a signed-in moviegoer and mint their JWT, as the other media tests do."""
    with SessionLocal() as db:
        user = User(phone=phone, name=None, created_at=datetime.now(timezone.utc))
        db.add(user)
        db.commit()
        return user.id, issue_token(user_id=user.id)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _asset(asset_id: int) -> MediaAsset | None:
    """The row itself, so an assertion can check what the route persisted."""
    with SessionLocal() as db:
        return db.get(MediaAsset, asset_id)


def _assets() -> list[MediaAsset]:
    with SessionLocal() as db:
        return list(db.scalars(select(MediaAsset).order_by(MediaAsset.id)))


def _upload_movie_media(
    client, kind: str, payload: bytes, content_type: str
) -> httpx.Response:
    """One multipart upload through the internal movie-media route."""
    return client.post(
        MOVIE_MEDIA_URL.format(movie_id=MOVIE_ID),
        files={"file": ("asset", payload, content_type)},
        data={"kind": kind},
        headers={"X-Internal-Token": MEDIA_SERVICE_TOKEN},
    )


def test_env_config_selects_the_minio_backend(monkeypatch) -> None:
    """The seven MINIO_* knobs reach their fields, and select the MinIO backend."""
    for name, value in (
        ("MEDIA_STORAGE_BACKEND", "minio"),
        ("MINIO_ENDPOINT", MINIO_ENDPOINT),
        ("MINIO_ACCESS_KEY", MINIO_ACCESS_KEY),
        ("MINIO_SECRET_KEY", MINIO_SECRET_KEY),
        ("MINIO_BUCKET", MINIO_BUCKET),
        ("MINIO_SECURE", "false"),
        ("MINIO_MEDIA_PREFIX", "/dev/media/"),
        ("MINIO_CREATE_BUCKET", "true"),
    ):
        monkeypatch.setenv(name, value)

    settings = MediaSettings.from_env()

    assert settings.storage_backend == "minio"
    assert settings.minio_endpoint == MINIO_ENDPOINT
    assert settings.minio_access_key == MINIO_ACCESS_KEY
    assert settings.minio_secret_key == MINIO_SECRET_KEY
    assert settings.minio_bucket == MINIO_BUCKET
    assert settings.minio_secure is False
    # The prefix keeps its slashes stripped, exactly as S3_MEDIA_PREFIX does.
    assert settings.minio_media_prefix == "dev/media"
    assert settings.minio_create_bucket is True

    backend = build_storage_backend(settings)

    assert isinstance(backend, MinioStorageBackend)
    # Building the backend never resolves a client, so this needs no boto3 or network.
    assert backend.settings.s3_bucket == MINIO_BUCKET
    assert backend.settings.s3_endpoint_url == MINIO_ENDPOINT_URL
    assert backend.settings.s3_access_key_id == MINIO_ACCESS_KEY
    assert backend.settings.s3_secret_access_key == MINIO_SECRET_KEY


def test_poster_backdrop_and_trailer_uploads_land_in_the_minio_bucket(client) -> None:
    """Each movie media kind is one private put_object into the MinIO bucket."""
    backend, fake = _backend()

    with use_minio_storage(backend):
        uploads = [
            _upload_movie_media(client, "POSTER", POSTER_PAYLOAD, "image/png"),
            _upload_movie_media(client, "BACKDROP", BACKDROP_PAYLOAD, "image/jpeg"),
            _upload_movie_media(client, "TRAILER", TRAILER_PAYLOAD, "video/mp4"),
        ]

    assert [response.status_code for response in uploads] == [201, 201, 201]
    assert [call["Bucket"] for call in fake.put_calls] == [MINIO_BUCKET] * 3
    # Private by default: no public ACL value is ever sent.
    assert [call["ACL"] for call in fake.put_calls] == [S3_PRIVATE_ACL] * 3
    assert [call["Body"] for call in fake.put_calls] == [
        POSTER_PAYLOAD,
        BACKDROP_PAYLOAD,
        TRAILER_PAYLOAD,
    ]
    assert [call["ContentType"] for call in fake.put_calls] == [
        "image/png",
        "image/jpeg",
        "video/mp4",
    ]

    for response, put in zip(uploads, fake.put_calls):
        asset = _asset(response.json()["assetId"])
        assert asset is not None
        # No prefix is configured, so the row's key is exactly the wire key.
        assert asset.storage_key == put["Key"]
        assert asset.storage_key.split("/")[0] in {"posters", "backdrops", "trailers"}
        assert not asset.storage_key.startswith("/")
        assert "//" not in asset.storage_key
        assert fake.objects[put["Key"]][0] == put["Body"]


def test_prefix_is_only_on_the_wire(client, monkeypatch) -> None:
    """MINIO_MEDIA_PREFIX reaches the wire key only; the row keeps the relative key."""
    monkeypatch.setenv("MINIO_MEDIA_PREFIX", "/dev/")
    prefix = MediaSettings.from_env().minio_media_prefix
    assert prefix == "dev"

    backend, fake = _backend(minio_media_prefix=prefix)

    with use_minio_storage(backend):
        response = _upload_movie_media(client, "POSTER", POSTER_PAYLOAD, "image/png")

    assert response.status_code == 201
    asset = _asset(response.json()["assetId"])
    assert asset is not None
    digest = hashlib.sha256(POSTER_PAYLOAD).hexdigest()
    assert asset.storage_key == f"posters/{digest}.png"
    assert not asset.storage_key.startswith("dev/")
    assert fake.put_calls[0]["Key"] == f"dev/{asset.storage_key}"
    assert fake.put_calls[0]["Bucket"] == MINIO_BUCKET

    wire_keys = {
        *[call["Key"] for call in fake.put_calls],
        *[call["Key"] for call in fake.get_calls],
        *[call["Key"] for call in fake.head_calls],
        *[call["params"]["Key"] for call in fake.presigned_calls],
    }

    assert wire_keys == {f"dev/{asset.storage_key}"}
    assert all("//" not in key and not key.startswith("/") for key in wire_keys)


def test_download_and_delete_go_through_minio(client) -> None:
    """The download route reads the object through MinIO, and a gone object is a 404."""
    backend, fake = _backend()

    with use_minio_storage(backend):
        uploaded = _upload_movie_media(client, "POSTER", POSTER_PAYLOAD, "image/png")
        assert uploaded.status_code == 201
        asset = _asset(uploaded.json()["assetId"])
        assert asset is not None

        served = client.get(DOWNLOAD_URL.format(asset_id=asset.id))

        assert served.status_code == 200
        assert served.content == POSTER_PAYLOAD
        assert fake.get_calls == [
            {"Bucket": MINIO_BUCKET, "Key": asset.storage_key, "Range": None}
        ]

        # No media route deletes an asset -- a superseded one keeps its row and its
        # file -- so the delete is issued through the same seam those routes read
        # through, and it must reach the MinIO bucket as `delete_object`.
        backend.delete(asset.storage_key)

        assert fake.delete_calls == [
            {"Bucket": MINIO_BUCKET, "Key": asset.storage_key}
        ]

        gone = client.get(DOWNLOAD_URL.format(asset_id=asset.id))

    assert gone.status_code == 404
    assert gone.json()["detail"] == f"Media asset not found: {asset.id}"
    # The missing object was decided by its head, never by reading it back.
    assert len(fake.get_calls) == 1


def test_avatar_presign_put_and_get(client) -> None:
    """An avatar goes straight into MinIO through a signed PUT, then reads back signed."""
    _, token = _user("+919000006001")
    auth = _auth(token)
    backend, fake = _backend()

    with use_minio_storage(backend):
        presigned = client.post(
            PRESIGN_URL,
            json={
                "purpose": "avatar",
                "filename": "me.png",
                "contentType": CONTENT_TYPE,
                "size": len(AVATAR_PAYLOAD),
            },
            headers=auth,
        )

        assert presigned.status_code == 201
        presign_body = presigned.json()
        asset_id = int(presign_body["assetId"])
        wire_key = f"avatars/{asset_id}.png"

        # The signed url is MinIO's own bucket, and it carries the configured expiry.
        assert presign_body["uploadUrl"].startswith(
            f"{MINIO_ENDPOINT_URL}/{MINIO_BUCKET}/{wire_key}?"
        )
        assert f"X-Amz-Expires={PRESIGN_EXPIRES_SECONDS}" in presign_body["uploadUrl"]
        put_call = fake.presigned_calls[0]
        assert put_call["method"] == "put_object"
        assert put_call["params"] == {
            "Bucket": MINIO_BUCKET,
            "Key": wire_key,
            "ContentType": CONTENT_TYPE,
        }
        # The presigned PUT is never handed a public ACL.
        assert "ACL" not in put_call["params"]

        # The browser PUTs straight at MinIO, so the fake bucket is what receives it.
        fake.put_object(
            Bucket=MINIO_BUCKET,
            Key=wire_key,
            Body=AVATAR_PAYLOAD,
            ContentType=CONTENT_TYPE,
        )

        put = fake.put_calls[0]
        assert put["Bucket"] == MINIO_BUCKET
        assert put["Key"] == wire_key
        assert put["Body"] == AVATAR_PAYLOAD
        assert put["ContentType"] == CONTENT_TYPE

        completed = client.post(COMPLETE_URL.format(asset_id=asset_id), headers=auth)

        assert completed.status_code == 200

        avatar = client.get(AVATAR_URL, headers=auth)

    assert avatar.status_code == 200
    read_body = avatar.json()
    assert read_body["assetId"] == asset_id
    assert read_body["url"].startswith(
        f"{MINIO_ENDPOINT_URL}/{MINIO_BUCKET}/{wire_key}?"
    )
    assert f"X-Amz-Expires={PRESIGN_EXPIRES_SECONDS}" in read_body["url"]

    get_call = fake.presigned_calls[-1]
    assert get_call["method"] == "get_object"
    assert get_call["params"] == {"Bucket": MINIO_BUCKET, "Key": wire_key}
    assert get_call["expires_in"] == app.state.media_settings.presign_expires_seconds

    stored = _asset(asset_id)
    assert stored is not None
    assert stored.storage_key == wire_key
    assert stored.sha256 == hashlib.sha256(AVATAR_PAYLOAD).hexdigest()
    assert stored.is_current is True


def test_review_photo_upload_lands_in_the_minio_bucket(client) -> None:
    """A review photo is one private put_object, and a non-author is refused first."""
    author_id, token = _user("+919000006002")
    backend, fake = _backend()

    with use_minio_storage(backend):
        uploaded = client.post(
            REVIEW_PHOTO_URL.format(movie_id=MOVIE_ID, review_id=REVIEW_ID),
            files={"file": ("photo", PHOTO_PAYLOAD, CONTENT_TYPE)},
            headers=_auth(token),
        )

        assert uploaded.status_code == 201
        put = fake.put_calls[0]
        assert put["Bucket"] == MINIO_BUCKET
        assert put["ACL"] == S3_PRIVATE_ACL
        assert put["Body"] == PHOTO_PAYLOAD
        assert put["Key"].startswith("review-photos/")
        assert put["ContentType"] == CONTENT_TYPE

        # The authorship check lives on the presign route: a caller who did not write
        # the review is refused there, before any byte is stored.
        _, stranger_token = _user("+919000006003")
        before = len(_assets())
        fake.put_calls.clear()

        with _use_reviews({REVIEW_ID: _review(REVIEW_ID, author_id)}):
            refused = client.post(
                PRESIGN_URL,
                json={
                    "purpose": "review_photo",
                    "ownerId": REVIEW_ID,
                    "filename": "photo.png",
                    "contentType": CONTENT_TYPE,
                    "size": len(PHOTO_PAYLOAD),
                },
                headers=_auth(stranger_token),
            )

    assert refused.status_code == 403
    assert refused.json()["detail"] == (
        "Only the review's author can upload a photo for this review"
    )
    assert fake.put_calls == []
    # The refusal created no pending asset row either.
    assert len(_assets()) == before


def test_health_reports_the_minio_endpoint_and_bucket(client) -> None:
    """Always a 200: the reachable and unreachable MinIO states are both reported."""
    backend, fake = _backend()

    with use_minio_storage(backend):
        reachable = client.get(HEALTH_URL)

        assert reachable.status_code == 200
        body = reachable.json()
        assert set(body) == HEALTH_KEYS
        assert body == {
            "backend": "minio",
            "reachable": True,
            "bucket": MINIO_BUCKET,
            "endpoint": MINIO_ENDPOINT_URL,
        }
        # Reachability is a real probe of the configured bucket, not a guess.
        assert fake.head_bucket_calls == [{"Bucket": MINIO_BUCKET}]

    broken_fake = FakeMinioClient(head_bucket_error=RuntimeError("endpoint down"))
    broken, _ = _backend(fake=broken_fake)

    with use_minio_storage(broken):
        unreachable = client.get(HEALTH_URL)

    # An unreachable endpoint is reported, never raised: the status stays 200.
    assert unreachable.status_code == 200
    assert set(unreachable.json()) == HEALTH_KEYS
    assert unreachable.json() == {
        "backend": "minio",
        "reachable": False,
        "bucket": MINIO_BUCKET,
        "endpoint": MINIO_ENDPOINT_URL,
    }

    # The override is gone, so the app's own backend answers again.
    local = client.get(HEALTH_URL)

    assert local.status_code == 200
    assert set(local.json()) == HEALTH_KEYS
    assert local.json()["backend"] == app.state.media_settings.storage_backend
