"""The S3 backing for media storage: its settings and the backend behind the seam.

The `S3_*` knobs are read with the same fall-back-not-raise convention as every other
settings class in the repo, so an absent, blank or unusable value selects the default
instead of raising: a mistyped `S3_SSE` turns encryption off rather than failing every
upload, and a prefix keeps its slashes stripped so joining a key never produces `//`.

The local backend is exercised against the same seam, because it is the one every
existing media route still runs on: the read-back methods (`open_stream`, `read_bytes`,
`exists`, `head`, `delete`) and the presigned upload record have to answer exactly what
a bucket-backed backend answers, and `integrity_report` has to work without a
filesystem path at all.

`S3StorageBackend` is checked against a hand-written fake client rather than moto, so the
suite needs neither boto3 nor a network: every recorded `put_object`, `get_object`,
`head_object`, `delete_object`, `head_bucket` and presigned call is asserted directly.
The same round trip runs against a real endpoint when `S3_INTEGRATION_ENDPOINT` names
one, and is skipped otherwise.
"""

import asyncio
import hashlib
import logging
import os
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.db import SessionLocal
from app.media import (
    DEFAULT_S3_MEDIA_PREFIX,
    DEFAULT_S3_REGION,
    SUPPORTED_SSE,
    EmptyUploadError,
    ExactSizeMismatchError,
    LocalStorageBackend,
    MEDIA_URL_PREFIX,
    MediaSettings,
    MediaTooLargeError,
    RequestStreamChunks,
    StorageHealth,
    StoredObject,
    build_storage_backend,
    get_storage_backend,
    integrity_report,
)
from app.main import app, create_app
from app.models import MediaAsset, User
from app.presign import decode_upload_token
from app.s3_storage import (
    S3_PRIVATE_ACL,
    S3_READ_CHUNK_SIZE,
    S3_SSE_HEADER,
    S3StorageBackend,
    build_s3_client,
)
from app.schemas import MediaKind
from app.security import issue_token
from app.streaming import STREAM_CHUNK_SIZE, ByteRange

# (environment variable, the field it feeds, a value it round trips).
S3_KNOBS: tuple[tuple[str, str, str], ...] = (
    ("S3_BUCKET", "s3_bucket", "bms-media"),
    ("S3_REGION", "s3_region", "eu-west-1"),
    ("S3_ENDPOINT_URL", "s3_endpoint_url", "http://localhost:4566"),
    ("S3_ACCESS_KEY_ID", "s3_access_key_id", "AKIAIOSFODNN7EXAMPLE"),
    ("S3_SECRET_ACCESS_KEY", "s3_secret_access_key", "wJalrXUtnFEMI/K7MDENG"),
    ("S3_MEDIA_PREFIX", "s3_media_prefix", "prod/media"),
    ("S3_SSE", "s3_sse", "AES256"),
)


def _clear_s3_env(monkeypatch) -> None:
    """Remove every S3_* knob, so a test reads only what it sets itself."""
    for name, _, _ in S3_KNOBS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(("name", "field", "value"), S3_KNOBS)
def test_s3_settings_read_every_env_knob(monkeypatch, name, field, value) -> None:
    """Exactly one variable set: exactly its own field carries the value."""
    _clear_s3_env(monkeypatch)
    monkeypatch.setenv(name, value)

    settings = MediaSettings.from_env()

    assert getattr(settings, field) == value


def test_s3_defaults_when_nothing_is_configured(monkeypatch) -> None:
    _clear_s3_env(monkeypatch)

    unset = MediaSettings.from_env()

    assert unset.s3_bucket == ""
    assert unset.s3_region == DEFAULT_S3_REGION == "us-east-1"
    assert unset.s3_endpoint_url == ""
    assert unset.s3_access_key_id == ""
    assert unset.s3_secret_access_key == ""
    assert unset.s3_media_prefix == DEFAULT_S3_MEDIA_PREFIX == ""
    assert unset.s3_sse is None

    # A blank variable reads as the default, exactly as a blank MEDIA_* one already does.
    for name, _, _ in S3_KNOBS:
        monkeypatch.setenv(name, "   ")

    blank = MediaSettings.from_env()

    assert blank.s3_bucket == ""
    assert blank.s3_region == DEFAULT_S3_REGION
    assert blank.s3_endpoint_url == ""
    assert blank.s3_access_key_id == ""
    assert blank.s3_secret_access_key == ""
    assert blank.s3_media_prefix == DEFAULT_S3_MEDIA_PREFIX
    assert blank.s3_sse is None


@pytest.mark.parametrize(
    ("raw", "expected"), [("AES256", "AES256"), ("", None), ("des-ede3", None)]
)
def test_s3_sse_only_accepts_aes256(monkeypatch, raw: str, expected: str | None) -> None:
    """Only a supported algorithm is kept; anything else turns encryption off."""
    monkeypatch.setenv("S3_SSE", raw)

    settings = MediaSettings.from_env()

    assert settings.s3_sse == expected
    assert SUPPORTED_SSE == frozenset({"AES256"})


def test_media_prefix_is_normalised(monkeypatch) -> None:
    """A configured prefix never starts or ends with a slash, so keys join cleanly."""
    for raw, expected in (
        ("/prod/media/", "prod/media"),
        ("prod/media", "prod/media"),
        ("/prod/media", "prod/media"),
        ("prod/media/", "prod/media"),
        ("", ""),
    ):
        monkeypatch.setenv("S3_MEDIA_PREFIX", raw)

        assert MediaSettings.from_env().s3_media_prefix == expected


AVATAR_BYTES = b"\x89PNG\r\n\x1a\navatar-bytes"
CONTENT_TYPE = "image/png"
MISSING_KEY = "avatars/no-such-object.png"


def _settings(tmp_path: Path) -> MediaSettings:
    """The local backend's settings, pointed at this test's temp directory."""
    return MediaSettings(
        storage_backend="local",
        upload_dir=str(tmp_path / "media"),
        max_image_bytes=1024,
        max_video_bytes=2048,
        service_token="test-service-token",
        presign_secret="test-presign-secret",
        presign_expires_seconds=900,
    )


async def _body(payload: bytes, chunk_size: int = 4) -> AsyncIterator[bytes]:
    """The chunks a server would hand storage for this body."""
    for start in range(0, len(payload), chunk_size):
        yield payload[start : start + chunk_size]


def _store(backend: LocalStorageBackend, payload: bytes) -> str:
    """Save one avatar through the backend and answer the key it landed on."""
    stored = asyncio.run(
        backend.save_exact(
            kind=MediaKind.AVATAR,
            content_type=CONTENT_TYPE,
            upload=RequestStreamChunks(_body(payload)),
            expected_size=len(payload),
        )
    )
    return stored.key


class _NoPathBackend:
    """A LocalStorageBackend wrapper that refuses every filesystem-path access.

    `integrity_report` used to hash through `path_for`; wrapping the backend this way
    proves it now reads through the seam's own `exists`/`open_stream` instead.
    """

    def __init__(self, inner: LocalStorageBackend) -> None:
        self._inner = inner

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def open_stream(
        self, key: str, byte_range: ByteRange | None = None
    ) -> Iterator[bytes]:
        return self._inner.open_stream(key, byte_range)

    def path_for(self, key: str) -> Path:
        raise AssertionError("integrity_report must not need a filesystem path")


def test_local_open_stream_streams_in_chunks(tmp_path) -> None:
    backend = LocalStorageBackend(_settings(tmp_path))
    payload = b"x" * (STREAM_CHUNK_SIZE * 2 + 7)
    key = _store(backend, payload)

    chunks = list(backend.open_stream(key))

    assert b"".join(chunks) == payload
    assert len(chunks) == 3
    assert all(len(chunk) <= STREAM_CHUNK_SIZE for chunk in chunks)
    # The object is handed back in pieces, never as one whole blob.
    assert chunks[0] != payload


def test_local_open_stream_honours_a_byte_range(tmp_path) -> None:
    backend = LocalStorageBackend(_settings(tmp_path))
    payload = bytes(range(10))
    key = _store(backend, payload)

    # No range is the whole object, exactly as the trailer route relies on.
    assert b"".join(backend.open_stream(key)) == payload
    assert b"".join(backend.open_stream(key, ByteRange(start=2, end=4))) == payload[2:5]
    assert (
        b"".join(backend.open_stream(key, ByteRange(start=0, end=len(payload) - 1)))
        == payload
    )


def test_local_exists_delete_and_head_round_trip(tmp_path) -> None:
    backend = LocalStorageBackend(_settings(tmp_path))
    key = _store(backend, AVATAR_BYTES)

    assert backend.exists(key) is True
    assert backend.read_bytes(key) == AVATAR_BYTES

    head = backend.head(key)

    assert head is not None
    assert head.size == len(AVATAR_BYTES)
    # A plain file reports no content type of its own; the row records the declared one.
    assert head.content_type is None

    assert backend.exists(MISSING_KEY) is False
    assert backend.head(MISSING_KEY) is None

    backend.delete(key)

    assert backend.exists(key) is False
    # Deleting an object that is already gone is not an error.
    backend.delete(key)


def test_integrity_report_works_without_path_for(tmp_path) -> None:
    inner = LocalStorageBackend(_settings(tmp_path))
    key = _store(inner, AVATAR_BYTES)
    digest = hashlib.sha256(AVATAR_BYTES).hexdigest()

    verified = integrity_report(
        storage=_NoPathBackend(inner),
        storage_key=key,
        expected_sha256=digest,
        expected_size=len(AVATAR_BYTES),
        algorithm="sha256",
    )

    assert verified.sha256 == digest
    assert verified.size == len(AVATAR_BYTES)
    assert verified.verified is True
    assert verified.recomputed_sha256 == digest
    assert verified.recomputed_size == len(AVATAR_BYTES)

    inner.delete(key)

    missing = integrity_report(
        storage=_NoPathBackend(inner),
        storage_key=key,
        expected_sha256=digest,
        expected_size=len(AVATAR_BYTES),
        algorithm="sha256",
    )

    # A gone object is the untrusted state, not an error.
    assert missing.verified is False
    assert missing.recomputed_sha256 is None
    assert missing.recomputed_size is None
    assert missing.sha256 == digest
    assert missing.size == len(AVATAR_BYTES)


def test_local_presigned_upload_carries_the_token_url(tmp_path) -> None:
    settings = _settings(tmp_path)
    backend = LocalStorageBackend(settings)

    presigned = backend.presigned_upload(
        asset_id=7,
        kind=MediaKind.AVATAR,
        size=len(AVATAR_BYTES),
        content_type=CONTENT_TYPE,
    )

    prefix = "/api/media/7/upload?token="
    assert presigned.url.startswith(prefix)
    assert presigned.method == "PUT"
    assert presigned.headers == {"Content-Type": CONTENT_TYPE}
    # The local save derives the key from the bytes, so there is no destination key.
    assert presigned.key == ""

    grant = decode_upload_token(settings, token=presigned.url[len(prefix) :])

    assert grant.asset_id == 7
    assert grant.kind is MediaKind.AVATAR
    assert grant.size == len(AVATAR_BYTES)
    assert grant.content_type == CONTENT_TYPE


S3_BUCKET = "bms-media"
S3_INTEGRATION_ENDPOINT = os.getenv("S3_INTEGRATION_ENDPOINT")


def _s3_settings(**overrides: object) -> MediaSettings:
    """S3-configured settings: one bucket, no prefix and no SSE unless a test says so."""
    values: dict[str, object] = {
        "storage_backend": "s3",
        "upload_dir": "/unused/local-media",
        "max_image_bytes": 1024,
        "max_video_bytes": 2048,
        "service_token": "test-service-token",
        "presign_secret": "test-presign-secret",
        "presign_expires_seconds": 900,
        "s3_bucket": S3_BUCKET,
        "s3_region": "us-east-1",
    }
    values.update(overrides)
    return MediaSettings(**values)  # type: ignore[arg-type]


@pytest.fixture()
def settings() -> MediaSettings:
    """The S3 backend's settings for a test that does not need a prefix or SSE."""
    return _s3_settings()


@pytest.fixture()
def fake_client() -> "FakeS3Client":
    """A fresh recording client, so every call one test makes is its own."""
    return FakeS3Client()


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


class FakeS3Client:
    """A hand-written stand-in for boto3's S3 client: in-memory objects, recorded calls.

    Every argument a real call would carry is recorded, so a test can assert the wire
    key (prefix included), the private ACL, the SSE parameter and the presigned expiry
    without a bucket or a network. A missing object raises the same `response`-shaped
    error botocore raises, which is what the backend's 404 handling reads.
    """

    def __init__(self, *, head_bucket_error: BaseException | None = None) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
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
            f"https://{bucket}.s3.amazonaws.com/{key}"
            f"?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=fake"
        )


class _FakeUpload:
    """The `read(size)` shape an UploadFile offers the save path, over a fixed payload."""

    def __init__(self, payload: bytes, chunk_size: int = 4) -> None:
        self._payload = payload
        self._chunk_size = chunk_size
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        limit = (
            self._chunk_size
            if size is None or size < 0
            else min(size, self._chunk_size)
        )
        chunk = self._payload[self._offset : self._offset + limit]
        self._offset += len(chunk)
        return chunk


def _save(
    backend: S3StorageBackend,
    payload: bytes,
    *,
    max_bytes: int = 1024,
    content_type: str = CONTENT_TYPE,
    chunk_size: int = 4,
) -> StoredObject:
    """Save one avatar through the backend, as the movie-media route would."""
    return asyncio.run(
        backend.save(
            kind=MediaKind.AVATAR,
            content_type=content_type,
            upload=_FakeUpload(payload, chunk_size),  # type: ignore[arg-type]
            max_bytes=max_bytes,
        )
    )


def _save_exact(
    backend: S3StorageBackend,
    payload: bytes,
    *,
    expected_size: int | None = None,
    content_type: str = CONTENT_TYPE,
) -> StoredObject:
    """Save one avatar through the exact-size path a direct upload uses."""
    return asyncio.run(
        backend.save_exact(
            kind=MediaKind.AVATAR,
            content_type=content_type,
            upload=RequestStreamChunks(_body(payload)),
            expected_size=len(payload) if expected_size is None else expected_size,
        )
    )


def test_save_puts_the_bytes_with_a_private_acl(settings, fake_client) -> None:
    backend = S3StorageBackend(settings, client=fake_client)

    stored = _save(backend, AVATAR_BYTES)

    assert stored.size == len(AVATAR_BYTES)
    assert stored.sha256 == hashlib.sha256(AVATAR_BYTES).hexdigest()
    assert stored.key == f"avatars/{stored.sha256}.png"

    assert len(fake_client.put_calls) == 1
    put = fake_client.put_calls[0]
    assert put["Bucket"] == S3_BUCKET
    assert put["Key"] == stored.key
    assert put["Body"] == AVATAR_BYTES
    assert put["ContentType"] == CONTENT_TYPE
    # Private by default: no public ACL value is ever sent.
    assert put["ACL"] == S3_PRIVATE_ACL == "private"
    assert all(call["ACL"] != "public-read" for call in fake_client.put_calls)


def test_save_refuses_an_empty_or_oversized_body(settings, fake_client) -> None:
    backend = S3StorageBackend(settings, client=fake_client)

    with pytest.raises(EmptyUploadError):
        _save(backend, b"")

    with pytest.raises(MediaTooLargeError) as caught:
        _save(backend, b"x" * 9, max_bytes=8)

    assert caught.value.limit == 8
    # A refused upload never reaches the bucket.
    assert fake_client.put_calls == []


@pytest.mark.parametrize("prefix", ["prod/media", ""])
def test_every_key_is_under_the_media_prefix(prefix: str) -> None:
    """Every wire key carries the configured prefix once, and nothing else does."""
    configured = _s3_settings(s3_media_prefix=prefix)
    fake_client = FakeS3Client()
    backend = S3StorageBackend(configured, client=fake_client)

    stored = _save(backend, AVATAR_BYTES)
    assert backend.exists(stored.key) is True
    assert backend.read_bytes(stored.key) == AVATAR_BYTES
    assert backend.head(stored.key) is not None
    assert b"".join(backend.open_stream(stored.key)) == AVATAR_BYTES
    backend.delete(stored.key)
    backend.public_url(stored.key)
    presigned = backend.presigned_upload(
        asset_id=7,
        kind=MediaKind.AVATAR,
        size=len(AVATAR_BYTES),
        content_type=CONTENT_TYPE,
    )

    def wire(key: str) -> str:
        return f"{prefix}/{key}" if prefix else key

    keys = {
        *[call["Key"] for call in fake_client.put_calls],
        *[call["Key"] for call in fake_client.get_calls],
        *[call["Key"] for call in fake_client.head_calls],
        *[call["Key"] for call in fake_client.delete_calls],
        *[call["params"]["Key"] for call in fake_client.presigned_calls],
    }

    assert keys == {wire(stored.key), wire(presigned.key)}
    assert all("//" not in key and not key.startswith("/") for key in keys)


@pytest.mark.parametrize(("sse", "expected"), [("AES256", "AES256"), (None, None)])
def test_sse_is_sent_only_when_configured(
    settings, fake_client, sse: str | None, expected: str | None
) -> None:
    backend = S3StorageBackend(replace(settings, s3_sse=sse), client=fake_client)

    stored = _save(backend, AVATAR_BYTES)
    assert stored.key.startswith("avatars/")

    put = fake_client.put_calls[0]
    assert put["ServerSideEncryption"] == expected

    presigned = backend.presigned_upload(
        asset_id=7,
        kind=MediaKind.AVATAR,
        size=len(AVATAR_BYTES),
        content_type=CONTENT_TYPE,
    )
    put_params = fake_client.presigned_calls[0]["params"]

    assert fake_client.presigned_calls[0]["method"] == "put_object"
    assert put_params.get("ServerSideEncryption") == expected
    if expected is None:
        assert S3_SSE_HEADER not in presigned.headers
    else:
        assert presigned.headers[S3_SSE_HEADER] == expected


@pytest.mark.parametrize("delta", [-1, 1])
def test_save_exact_refuses_a_body_off_the_declared_size(
    settings, fake_client, delta: int
) -> None:
    backend = S3StorageBackend(settings, client=fake_client)

    stored = _save_exact(backend, AVATAR_BYTES)

    assert stored.size == len(AVATAR_BYTES)
    assert stored.sha256 == hashlib.sha256(AVATAR_BYTES).hexdigest()
    assert fake_client.put_calls[0]["Key"] == stored.key

    fake_client.put_calls.clear()
    expected = len(AVATAR_BYTES) + delta

    with pytest.raises(ExactSizeMismatchError) as caught:
        _save_exact(backend, AVATAR_BYTES, expected_size=expected)

    assert caught.value.expected == expected
    assert caught.value.actual == len(AVATAR_BYTES)
    # Neither a short nor a long body is ever put.
    assert fake_client.put_calls == []


def test_open_stream_honours_a_byte_range(settings, fake_client) -> None:
    backend = S3StorageBackend(settings, client=fake_client)
    payload = bytes(range(10))
    key = _save_exact(backend, payload).key

    assert b"".join(backend.open_stream(key)) == payload
    # No range means no Range header: the whole object comes back.
    assert fake_client.get_calls[-1]["Range"] is None

    assert b"".join(backend.open_stream(key, ByteRange(start=2, end=4))) == payload[2:5]
    assert fake_client.get_calls[-1]["Range"] == "bytes=2-4"

    large = b"y" * (S3_READ_CHUNK_SIZE * 2 + 7)
    large_key = _save(
        backend, large, max_bytes=len(large), chunk_size=S3_READ_CHUNK_SIZE
    ).key

    chunks = list(backend.open_stream(large_key))

    assert b"".join(chunks) == large
    assert len(chunks) == 3
    assert all(len(chunk) <= S3_READ_CHUNK_SIZE for chunk in chunks)


def test_exists_delete_and_head(settings, fake_client) -> None:
    backend = S3StorageBackend(settings, client=fake_client)
    key = _save(backend, AVATAR_BYTES).key

    assert backend.exists(key) is True
    assert backend.read_bytes(key) == AVATAR_BYTES

    head = backend.head(key)

    assert head is not None
    assert head.size == len(AVATAR_BYTES)
    assert head.content_type == CONTENT_TYPE

    assert backend.exists(MISSING_KEY) is False
    assert backend.head(MISSING_KEY) is None

    backend.delete(key)

    assert backend.exists(key) is False
    # Deleting an object that is already gone is not an error.
    backend.delete(key)


def test_presigned_get_and_put_use_the_configured_expiry(
    settings, fake_client
) -> None:
    backend = S3StorageBackend(settings, client=fake_client)

    url = backend.public_url("avatars/x.png")

    get_call = fake_client.presigned_calls[0]
    assert get_call["method"] == "get_object"
    assert get_call["expires_in"] == settings.presign_expires_seconds == 900
    assert get_call["params"] == {"Bucket": S3_BUCKET, "Key": "avatars/x.png"}
    assert url.startswith(f"https://{S3_BUCKET}.s3.amazonaws.com/avatars/x.png?")
    assert "X-Amz-Expires=900" in url

    presigned = backend.presigned_upload(
        asset_id=7,
        kind=MediaKind.AVATAR,
        size=len(AVATAR_BYTES),
        content_type=CONTENT_TYPE,
    )

    put_call = fake_client.presigned_calls[1]
    assert put_call["method"] == "put_object"
    assert put_call["expires_in"] == 900
    assert put_call["params"] == {
        "Bucket": S3_BUCKET,
        "Key": "avatars/7.png",
        "ContentType": CONTENT_TYPE,
    }
    # A presigned PUT is never handed a public ACL: the object stays private.
    assert "ACL" not in put_call["params"]

    assert presigned.method == "PUT"
    assert presigned.key == "avatars/7.png"
    assert presigned.headers == {"Content-Type": CONTENT_TYPE}
    assert "X-Amz-Expires=900" in presigned.url


def test_health_reports_reachable_and_unreachable(settings) -> None:
    healthy = S3StorageBackend(settings, client=FakeS3Client())

    assert healthy.health() == StorageHealth(backend="s3", reachable=True)

    denied = S3StorageBackend(
        settings, client=FakeS3Client(head_bucket_error=_ClientError("403", 403))
    )

    assert denied.health() == StorageHealth(backend="s3", reachable=False)

    # A broker that is simply gone is reported down, never raised out of the probe.
    broken = S3StorageBackend(
        settings, client=FakeS3Client(head_bucket_error=RuntimeError("endpoint down"))
    )

    assert broken.health() == StorageHealth(backend="s3", reachable=False)


@pytest.mark.skipif(
    not S3_INTEGRATION_ENDPOINT,
    reason="set S3_INTEGRATION_ENDPOINT to a live S3 or LocalStack endpoint to run",
)
def test_localstack_round_trip() -> None:
    """The same round trip against a real endpoint: LocalStack, MinIO or S3 itself."""
    settings = _s3_settings(
        s3_bucket=os.getenv("S3_INTEGRATION_BUCKET", S3_BUCKET),
        s3_endpoint_url=S3_INTEGRATION_ENDPOINT,
        s3_access_key_id=os.getenv("S3_INTEGRATION_ACCESS_KEY_ID", "test"),
        s3_secret_access_key=os.getenv("S3_INTEGRATION_SECRET_ACCESS_KEY", "test"),
        s3_media_prefix="integration",
    )
    backend = S3StorageBackend(settings)
    stored = _save(backend, AVATAR_BYTES)
    try:
        assert backend.exists(stored.key) is True
        assert backend.read_bytes(stored.key) == AVATAR_BYTES
        assert b"".join(backend.open_stream(stored.key)) == AVATAR_BYTES
        assert (
            b"".join(backend.open_stream(stored.key, ByteRange(start=0, end=3)))
            == AVATAR_BYTES[:4]
        )
        head = backend.head(stored.key)
        assert head is not None
        assert head.size == len(AVATAR_BYTES)
        assert backend.public_url(stored.key).startswith("http")
        presigned = backend.presigned_upload(
            asset_id=7,
            kind=MediaKind.AVATAR,
            size=len(AVATAR_BYTES),
            content_type=CONTENT_TYPE,
        )
        assert presigned.url.startswith("http")
        assert presigned.key == "avatars/7.png"
        assert backend.health().reachable is True
    finally:
        backend.delete(stored.key)

    assert backend.exists(stored.key) is False


def test_s3_is_selected_by_media_storage_backend(settings) -> None:
    """MEDIA_STORAGE_BACKEND=s3 with a bucket configured selects the S3 backend."""
    backend = build_storage_backend(settings)

    assert isinstance(backend, S3StorageBackend)
    assert backend.settings is settings


def test_unknown_backend_falls_back_to_local(tmp_path, caplog) -> None:
    """A backend name this build does not know keeps the current local behaviour."""
    configured = replace(_settings(tmp_path), storage_backend="minio")

    with caplog.at_level(logging.WARNING, logger="app.media"):
        backend = build_storage_backend(configured)

    assert isinstance(backend, LocalStorageBackend)
    assert backend.settings is configured
    assert any("minio" in record.getMessage() for record in caplog.records)


def test_s3_without_a_bucket_falls_back_to_local(tmp_path, caplog) -> None:
    """s3 with no bucket is a configuration mistake, not a usable backend."""
    configured = replace(
        _s3_settings(upload_dir=str(tmp_path / "media")), s3_bucket=""
    )

    with caplog.at_level(logging.WARNING, logger="app.media"):
        backend = build_storage_backend(configured)

    assert isinstance(backend, LocalStorageBackend)
    assert backend.settings is configured
    assert any("S3_BUCKET" in record.getMessage() for record in caplog.records)


def test_repeated_backends_reuse_one_s3_client(monkeypatch) -> None:
    """The backend is built per request, so the client it builds is memoised."""
    built: list[dict[str, object]] = []

    class _FakeBoto3:
        @staticmethod
        def client(name: str, **options: object) -> object:
            built.append({"name": name, "options": options})
            return object()

    settings = _s3_settings(s3_media_prefix="reuse")
    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)
    build_s3_client.cache_clear()
    try:
        first = build_storage_backend(settings)
        second = build_storage_backend(settings)

        assert isinstance(first, S3StorageBackend)
        assert isinstance(second, S3StorageBackend)
        assert first is not second
        # One client for the configuration, shared by both backends.
        assert first.client is second.client
        assert built == [{"name": "s3", "options": {"region_name": settings.s3_region}}]
    finally:
        build_s3_client.cache_clear()


def test_create_app_skips_the_static_mount_for_s3(monkeypatch, tmp_path) -> None:
    """An S3 deployment has no upload tree to create and no /media mount to serve."""
    upload_dir = tmp_path / "s3-media"
    monkeypatch.setenv("MEDIA_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", "bms-media")
    monkeypatch.setenv("MEDIA_UPLOAD_DIR", str(upload_dir))

    remote = create_app()

    assert remote.state.media_settings.storage_backend == "s3"
    assert upload_dir.exists() is False
    assert MEDIA_URL_PREFIX not in {
        getattr(route, "path", "") for route in remote.routes
    }

    # The local backend keeps creating the tree and mounting it.
    monkeypatch.setenv("MEDIA_STORAGE_BACKEND", "local")

    local = create_app()

    assert local.state.media_settings.storage_backend == "local"
    assert upload_dir.is_dir()
    assert (upload_dir / "posters").is_dir()
    assert MEDIA_URL_PREFIX in {getattr(route, "path", "") for route in local.routes}


PRESIGN_URL = "/api/media/presign"
COMPLETE_URL = "/api/media/{asset_id}/complete"


@contextmanager
def _use_storage(storage: object) -> Iterator[object]:
    """Serve this backend to the media routes; the override is always popped again."""
    app.dependency_overrides[get_storage_backend] = lambda: storage
    try:
        yield storage
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def _user(phone: str) -> tuple[int, str]:
    """Insert a signed-in moviegoer and mint their JWT, as the other media tests do."""
    with SessionLocal() as db:
        user = User(phone=phone, name=None)
        db.add(user)
        db.commit()
        return user.id, issue_token(user_id=user.id)


def test_s3_presign_records_the_destination_key(settings, fake_client) -> None:
    """The signed PUT names a key built from the asset id, under the media prefix."""
    backend = S3StorageBackend(
        replace(settings, s3_media_prefix="prod"), client=fake_client
    )

    presigned = backend.presigned_upload(
        asset_id=7,
        kind=MediaKind.AVATAR,
        size=len(AVATAR_BYTES),
        content_type=CONTENT_TYPE,
    )

    # The key the row records is backend-relative; the prefix is added only on the wire.
    assert presigned.key == "avatars/7.png"
    assert presigned.method == "PUT"
    assert presigned.headers == {"Content-Type": CONTENT_TYPE}

    put_call = fake_client.presigned_calls[0]
    assert put_call["method"] == "put_object"
    assert put_call["expires_in"] == settings.presign_expires_seconds
    assert put_call["params"] == {
        "Bucket": S3_BUCKET,
        "Key": "prod/avatars/7.png",
        "ContentType": CONTENT_TYPE,
    }
    # A presigned PUT never carries a public ACL: the object stays private.
    assert "ACL" not in put_call["params"]


def test_complete_heads_the_object_and_stores_the_digest(
    client, settings, fake_client
) -> None:
    """A browser PUTs straight into the bucket, and completion verifies what landed.

    Nothing is taken from the client: the size and content type come from
    `head_object`, the digest is recomputed from the stored bytes, and only then does
    the row become the caller's current avatar.
    """
    backend = S3StorageBackend(settings, client=fake_client)
    user_id, token = _user("+919000009001")
    auth = {"Authorization": f"Bearer {token}"}

    with _use_storage(backend):
        presigned = client.post(
            PRESIGN_URL,
            json={
                "purpose": "avatar",
                "filename": "me.png",
                "contentType": CONTENT_TYPE,
                "size": len(AVATAR_BYTES),
            },
            headers=auth,
        )

        assert presigned.status_code == 201
        body = presigned.json()
        asset_id = int(body["assetId"])
        # The url is the bucket's own signed PUT, not this API's /upload route.
        assert body["uploadUrl"].startswith("https://")
        assert "/upload?token=" not in body["uploadUrl"]

        wire_key = f"avatars/{asset_id}.png"
        # The browser uploads directly; the fake client is the bucket it lands in.
        fake_client.put_object(
            Bucket=S3_BUCKET,
            Key=wire_key,
            Body=AVATAR_BYTES,
            ContentType=CONTENT_TYPE,
            ACL=S3_PRIVATE_ACL,
        )

        completed = client.post(
            COMPLETE_URL.format(asset_id=asset_id), headers=auth
        )

    assert completed.status_code == 200
    completed_body = completed.json()
    assert set(completed_body) == {
        "assetId",
        "kind",
        "url",
        "size",
        "contentType",
        "sha256",
    }
    assert completed_body["assetId"] == asset_id
    assert completed_body["kind"] == MediaKind.AVATAR.value
    assert completed_body["size"] == len(AVATAR_BYTES)
    assert completed_body["contentType"] == CONTENT_TYPE
    assert completed_body["sha256"] == hashlib.sha256(AVATAR_BYTES).hexdigest()
    # The url is a freshly signed GET, because the object itself stays private.
    assert completed_body["url"].startswith(
        f"https://{S3_BUCKET}.s3.amazonaws.com/{wire_key}?"
    )

    with SessionLocal() as db:
        stored = db.get(MediaAsset, asset_id)
        assert stored is not None
        assert stored.user_id == user_id
        assert stored.storage_key == wire_key
        assert stored.size_bytes == len(AVATAR_BYTES)
        assert stored.sha256 == hashlib.sha256(AVATAR_BYTES).hexdigest()
        assert stored.is_current is True
        assert stored.verified is True
        assert stored.uploaded_at is not None

    # The verification was a head of exactly the object the presign named.
    assert fake_client.head_calls == [{"Bucket": S3_BUCKET, "Key": wire_key}]

