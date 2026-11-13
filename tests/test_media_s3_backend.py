"""The S3 backend, driven against a fake boto3 client so no bucket is needed.

Every operation is asserted on what the adapter asked the client to do -- the
`put_object`/`get_object`/`head_object`/`delete_object`/`head_bucket`/
`generate_presigned_url` kwargs -- rather than on a real S3 answer, exactly as
tests/test_events_rabbit.py stands a recorder in for the pika channel. The fake holds
`{key: bytes}` in memory, so "exists", "the object's bytes" and "deleted" all mean the
same thing they would against LocalStack.

The async operations are driven with `asyncio.run` because the suite has no async test
plugin, the way tests/test_media_local_backend.py drives the local backend. The
`S3StorageBackend` is built over `dataclasses.replace(MediaSettings.from_env(), ...)`,
so no test touches `app.state`.
"""

import asyncio
import hashlib
import io
from collections.abc import AsyncIterator
from dataclasses import replace
from urllib.parse import urlencode

import pytest

import app.media_s3 as media_s3
from app.media import (
    CHUNK_SIZE,
    DigestResult,
    EmptyUploadError,
    MediaSettings,
    MediaTooLargeError,
    StoredObject,
)
from app.media_s3 import (
    S3_ACL_PRIVATE,
    S3_STORAGE_BACKEND_NAME,
    S3StorageBackend,
    normalized_prefix,
    s3_object_key,
)
from app.schemas import MediaKind

BUCKET = "bms-media"
REGION = "ap-south-1"
PREFIX = "media"
CONTENT_TYPE = "image/png"
ASSET_ID = 7
EXPIRES_SECONDS = 300
PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\navatar-bytes"
BIG_PAYLOAD = b"x" * (CHUNK_SIZE + 512)
# How boto3 renders a put's params as query parameters of a presigned url.
S3_QUERY_PARAM_NAMES = {
    "ServerSideEncryption": "x-amz-server-side-encryption",
    "ContentType": "Content-Type",
}


class _MissingKey(Exception):
    """The error botocore raises for a head/get on a key the bucket does not hold."""

    def __init__(self, key: str) -> None:
        super().__init__(f"NoSuchKey: {key}")
        self.response = {
            "Error": {
                "Code": "NoSuchKey",
                "Message": "The specified key does not exist.",
            },
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }


class FakeS3Client:
    """Stand-in for a boto3 S3 client: an in-memory object store that records every call."""

    def __init__(self, *, raise_on_health: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.raise_on_health = raise_on_health
        self.put_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []
        self.head_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.health_calls: list[dict[str, object]] = []
        self.presign_calls: list[dict[str, object]] = []

    def put_object(self, **kwargs):
        body = kwargs.pop("Body")
        payload = body.read() if hasattr(body, "read") else bytes(body)
        self.put_calls.append({**kwargs, "Body": payload})
        self.objects[str(kwargs["Key"])] = payload
        return {"ETag": '"fake-etag"'}

    def get_object(self, **kwargs):
        self.get_calls.append(dict(kwargs))
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise _MissingKey(key)
        payload = self.objects[key]
        byte_range = kwargs.get("Range")
        if byte_range is not None:
            spec = str(byte_range).partition("=")[2]
            start_text, _, end_text = spec.partition("-")
            start = int(start_text)
            end = len(payload) - 1 if not end_text else int(end_text)
            payload = payload[start : end + 1]
        return {"Body": io.BytesIO(payload), "ContentLength": len(payload)}

    def head_object(self, **kwargs):
        self.head_calls.append(dict(kwargs))
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise _MissingKey(key)
        return {"ContentLength": len(self.objects[key])}

    def delete_object(self, **kwargs):
        self.delete_calls.append(dict(kwargs))
        self.objects.pop(str(kwargs["Key"]), None)
        return {}

    def head_bucket(self, **kwargs):
        self.health_calls.append(dict(kwargs))
        if self.raise_on_health:
            raise RuntimeError("no bucket reachable")
        return {}

    def generate_presigned_url(self, operation, *, Params=None, ExpiresIn=None):
        params = dict(Params or {})
        self.presign_calls.append(
            {"operation": operation, "Params": params, "ExpiresIn": ExpiresIn}
        )
        query = {
            S3_QUERY_PARAM_NAMES.get(name, name): value
            for name, value in params.items()
            if name not in {"Bucket", "Key"}
        }
        url = f"https://{params.get('Bucket')}.s3.amazonaws.com/{params.get('Key')}"
        if query:
            url = f"{url}?{urlencode(query)}"
        return url


def _settings(**overrides: object) -> MediaSettings:
    """The environment's settings with the S3 knobs replaced; app.state is untouched."""
    base = replace(
        MediaSettings.from_env(),
        storage_backend="s3",
        s3_bucket=BUCKET,
        s3_region=REGION,
        s3_endpoint_url="",
        s3_access_key_id="media-key",
        s3_secret_access_key="media-secret",
        s3_media_prefix=PREFIX,
        s3_sse="",
    )
    return replace(base, **overrides)


@pytest.fixture()
def s3_client() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture()
def s3_backend(s3_client: FakeS3Client) -> tuple[S3StorageBackend, FakeS3Client]:
    """The backend over a fake client, so nothing here needs a bucket."""
    return S3StorageBackend(_settings(), client=s3_client), s3_client


async def _chunks(payload: bytes) -> AsyncIterator[bytes]:
    """One chunk, so `save_stream` sees exactly this payload."""
    yield payload


async def _collect(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in chunks])


def _store(
    backend: S3StorageBackend,
    payload: bytes = PHOTO_PAYLOAD,
    *,
    kind: MediaKind = MediaKind.AVATAR,
    content_type: str = CONTENT_TYPE,
) -> StoredObject:
    return asyncio.run(
        backend.save_stream(
            kind=kind,
            content_type=content_type,
            chunks=_chunks(payload),
            max_bytes=len(payload),
        )
    )


def test_save_stream_puts_the_bytes_under_the_media_prefix(
    s3_backend: tuple[S3StorageBackend, FakeS3Client],
) -> None:
    backend, fake = s3_backend

    stored = _store(backend)

    assert stored.size == len(PHOTO_PAYLOAD)
    assert stored.sha256 == hashlib.sha256(PHOTO_PAYLOAD).hexdigest()
    assert stored.key.startswith(f"{PREFIX}/avatars/")
    assert not stored.key.startswith("/")
    assert stored.key.endswith(".png")

    assert len(fake.put_calls) == 1
    put = fake.put_calls[0]
    assert put["Bucket"] == BUCKET
    assert put["Key"] == stored.key
    assert put["Body"] == PHOTO_PAYLOAD
    assert fake.objects[stored.key] == PHOTO_PAYLOAD


def test_save_stream_enforces_the_byte_limit_and_refuses_an_empty_stream(
    s3_backend: tuple[S3StorageBackend, FakeS3Client],
) -> None:
    backend, fake = s3_backend

    with pytest.raises(MediaTooLargeError):
        asyncio.run(
            backend.save_stream(
                kind=MediaKind.AVATAR,
                content_type=CONTENT_TYPE,
                chunks=_chunks(PHOTO_PAYLOAD),
                max_bytes=len(PHOTO_PAYLOAD) - 1,
            )
        )

    with pytest.raises(EmptyUploadError):
        asyncio.run(
            backend.save_stream(
                kind=MediaKind.AVATAR,
                content_type=CONTENT_TYPE,
                chunks=_chunks(b""),
                max_bytes=len(PHOTO_PAYLOAD),
            )
        )

    # A refused upload leaves no object behind at all.
    assert fake.put_calls == []
    assert fake.objects == {}


def test_iter_chunks_and_digest_read_the_stored_object(
    s3_backend: tuple[S3StorageBackend, FakeS3Client],
) -> None:
    backend, fake = s3_backend
    stored = _store(backend, BIG_PAYLOAD)

    whole = asyncio.run(_collect(backend.iter_chunks(key=stored.key)))

    assert whole == BIG_PAYLOAD
    # The whole object is one streamed GET, read in bounded chunks.
    assert len(fake.get_calls) == 1
    assert "Range" not in fake.get_calls[0]

    recomputed = asyncio.run(backend.digest(key=stored.key, algorithm="sha256"))

    assert isinstance(recomputed, DigestResult)
    assert recomputed.sha256 == stored.sha256
    assert recomputed.sha256 == hashlib.sha256(BIG_PAYLOAD).hexdigest()
    assert recomputed.size == stored.size == len(BIG_PAYLOAD)
    assert len(fake.get_calls) == 2


def test_iter_chunks_honours_an_offset_and_a_length(
    s3_backend: tuple[S3StorageBackend, FakeS3Client],
) -> None:
    backend, fake = s3_backend
    stored = _store(backend, BIG_PAYLOAD)
    offset = CHUNK_SIZE - 10
    length = 20

    sliced = asyncio.run(
        _collect(backend.iter_chunks(key=stored.key, offset=offset, length=length))
    )

    assert sliced == BIG_PAYLOAD[offset : offset + length]
    assert fake.get_calls[-1]["Range"] == f"bytes={offset}-{offset + length - 1}"

    tail = asyncio.run(_collect(backend.iter_chunks(key=stored.key, offset=CHUNK_SIZE)))

    assert tail == BIG_PAYLOAD[CHUNK_SIZE:]
    assert fake.get_calls[-1]["Range"] == f"bytes={CHUNK_SIZE}-"


def test_digest_and_exists_answer_none_and_false_for_a_missing_key(
    s3_backend: tuple[S3StorageBackend, FakeS3Client],
) -> None:
    backend, _ = s3_backend

    assert asyncio.run(backend.exists(key="media/avatars/never-written.png")) is False
    assert (
        asyncio.run(
            backend.digest(key="media/avatars/never-written.png", algorithm="sha256")
        )
        is None
    )


def test_exists_is_true_for_a_stored_key_and_false_after_delete(
    s3_backend: tuple[S3StorageBackend, FakeS3Client],
) -> None:
    backend, fake = s3_backend
    stored = _store(backend)

    assert asyncio.run(backend.exists(key=stored.key)) is True
    assert fake.head_calls[-1] == {"Bucket": BUCKET, "Key": stored.key}

    asyncio.run(backend.delete(key=stored.key))

    assert fake.delete_calls == [{"Bucket": BUCKET, "Key": stored.key}]
    assert asyncio.run(backend.exists(key=stored.key)) is False
    # Deleting a key that is already gone is not an error.
    asyncio.run(backend.delete(key=stored.key))


def test_presigned_upload_url_is_a_put_on_the_bucket_with_the_content_type(
    s3_backend: tuple[S3StorageBackend, FakeS3Client],
) -> None:
    backend, fake = s3_backend
    key = f"{PREFIX}/avatars/reserved.png"

    presigned = backend.presigned_upload_url(
        asset_id=ASSET_ID,
        key=key,
        content_type=CONTENT_TYPE,
        expires_seconds=EXPIRES_SECONDS,
    )

    assert presigned.method == "PUT"
    assert presigned.headers == {"Content-Type": CONTENT_TYPE}
    assert presigned.url.startswith(f"https://{BUCKET}.s3.amazonaws.com/{key}?")

    assert len(fake.presign_calls) == 1
    call = fake.presign_calls[0]
    assert call["operation"] == "put_object"
    assert call["ExpiresIn"] == EXPIRES_SECONDS
    assert call["Params"] == {
        "Bucket": BUCKET,
        "Key": key,
        "ContentType": CONTENT_TYPE,
    }


def test_presigned_download_url_is_a_get_on_the_bucket(
    s3_backend: tuple[S3StorageBackend, FakeS3Client],
) -> None:
    backend, fake = s3_backend
    stored = _store(backend)

    url = backend.presigned_download_url(key=stored.key, expires_seconds=60)

    assert url == f"https://{BUCKET}.s3.amazonaws.com/{stored.key}"
    call = fake.presign_calls[-1]
    assert call["operation"] == "get_object"
    assert call["ExpiresIn"] == 60
    assert call["Params"] == {"Bucket": BUCKET, "Key": stored.key}


def test_every_put_is_private(s3_client: FakeS3Client) -> None:
    backend = S3StorageBackend(_settings(), client=s3_client)

    _store(backend)
    _store(backend, b"\x89PNG\r\n\x1a\nsecond-avatar")

    assert [put["ACL"] for put in s3_client.put_calls] == [
        S3_ACL_PRIVATE,
        S3_ACL_PRIVATE,
    ]


def test_sse_is_carried_by_the_put_and_the_presigned_url_only_when_configured(
    s3_client: FakeS3Client,
) -> None:
    plain = S3StorageBackend(_settings(), client=s3_client)
    encrypted = S3StorageBackend(_settings(s3_sse="AES256"), client=s3_client)

    plain_stored = _store(plain)
    plain_url = plain.presigned_upload_url(
        asset_id=ASSET_ID,
        key=plain_stored.key,
        content_type=CONTENT_TYPE,
        expires_seconds=EXPIRES_SECONDS,
    )

    assert "ServerSideEncryption" not in s3_client.put_calls[0]
    assert s3_client.presign_calls[0]["Params"] == {
        "Bucket": BUCKET,
        "Key": plain_stored.key,
        "ContentType": CONTENT_TYPE,
    }
    assert "x-amz-server-side-encryption" not in plain_url.url

    encrypted_stored = _store(encrypted)
    encrypted_url = encrypted.presigned_upload_url(
        asset_id=ASSET_ID,
        key=encrypted_stored.key,
        content_type=CONTENT_TYPE,
        expires_seconds=EXPIRES_SECONDS,
    )

    assert s3_client.put_calls[1]["ServerSideEncryption"] == "AES256"
    assert s3_client.presign_calls[1]["Params"]["ServerSideEncryption"] == "AES256"
    assert "x-amz-server-side-encryption=AES256" in encrypted_url.url


def test_prefix_is_normalised_without_leading_or_trailing_slashes(
    s3_client: FakeS3Client,
) -> None:
    assert normalized_prefix("/uploads/") == "uploads"
    assert normalized_prefix("  /uploads/  ") == "uploads"
    assert normalized_prefix("") == PREFIX
    assert normalized_prefix("/") == PREFIX

    backend = S3StorageBackend(_settings(s3_media_prefix="/uploads/"), client=s3_client)
    stored = _store(backend, kind=MediaKind.REVIEW_PHOTO)

    assert stored.key.startswith("uploads/review-photos/")
    assert not stored.key.startswith("/")
    assert "//" not in stored.key
    assert s3_client.put_calls[0]["Key"] == stored.key


def test_prepare_key_reserves_a_unique_key_of_the_right_kind() -> None:
    settings = _settings()

    first = s3_object_key(
        settings=settings,
        kind=MediaKind.POSTER,
        content_type="image/png",
        name="abcdef",
    )

    assert first == f"{PREFIX}/posters/abcdef.png"

    backend = S3StorageBackend(settings, client=FakeS3Client())
    reserved = backend.prepare_key(kind=MediaKind.TRAILER, content_type="video/mp4")

    assert reserved.startswith(f"{PREFIX}/trailers/")
    assert reserved.endswith(".mp4")
    assert reserved != backend.prepare_key(
        kind=MediaKind.TRAILER, content_type="video/mp4"
    )


def test_public_url_stays_the_app_proxy_path(
    s3_backend: tuple[S3StorageBackend, FakeS3Client],
) -> None:
    backend, _ = s3_backend
    stored = _store(backend)

    assert backend.public_url(stored.key) == f"/media/{stored.key}"


def test_health_reports_the_bucket_and_follows_head_bucket() -> None:
    healthy = FakeS3Client()
    reachable = S3StorageBackend(_settings(), client=healthy).health()

    assert reachable.backend == S3_STORAGE_BACKEND_NAME == "s3"
    assert reachable.bucket == BUCKET
    assert reachable.reachable is True
    assert healthy.health_calls == [{"Bucket": BUCKET}]

    broken = FakeS3Client(raise_on_health=True)
    unreachable = S3StorageBackend(_settings(), client=broken).health()

    assert unreachable.backend == "s3"
    assert unreachable.bucket == BUCKET
    assert unreachable.reachable is False


def test_an_injected_client_needs_no_usable_endpoint_or_region() -> None:
    # A malformed endpoint must not raise at import time or when a client is injected;
    # only build_s3_client would ever hand it to boto3.
    settings = _settings(s3_endpoint_url="not a url //", s3_region="", s3_bucket="")
    client = FakeS3Client()

    backend = S3StorageBackend(settings, client=client)

    assert backend.settings.s3_endpoint_url == "not a url //"
    stored = _store(backend)
    assert client.put_calls[0]["Bucket"] == ""

    health = backend.health()

    # No bucket to probe is reported as unreachable rather than as a broken probe.
    assert health.bucket is None
    assert health.reachable is False
    assert client.health_calls == []


def test_a_storage_failure_surfaces_instead_of_being_swallowed() -> None:
    class _BrokenClient(FakeS3Client):
        def put_object(self, **kwargs):
            raise RuntimeError("bucket is gone")

    backend = S3StorageBackend(_settings(), client=_BrokenClient())

    with pytest.raises(RuntimeError):
        _store(backend)


def test_build_s3_client_is_only_reached_when_no_client_is_injected(monkeypatch) -> None:
    """The adapter never talks to boto3 itself, so a fake client needs no credentials."""
    calls: list[MediaSettings] = []

    def _refuse(settings: MediaSettings) -> object:
        calls.append(settings)
        raise RuntimeError("boto3 must not be reached")

    monkeypatch.setattr(media_s3, "build_s3_client", _refuse)

    injected = S3StorageBackend(_settings(), client=FakeS3Client())

    assert calls == []
    assert injected.public_url("media/avatars/a.png") == "/media/media/avatars/a.png"

    with pytest.raises(RuntimeError):
        S3StorageBackend(_settings())

    assert len(calls) == 1
