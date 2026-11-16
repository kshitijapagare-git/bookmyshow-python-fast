"""The Google Cloud Storage backing for media storage: its settings and the backend.

The `GCS_*` knobs are read with the same fall-back-not-raise convention as every other
settings class in the repo, so an absent or blank value selects the default instead of
raising: a blank `GCS_BUCKET` reads as "GCS is not configured" rather than a bucket named
"", and a prefix keeps its slashes stripped so joining a key never produces `//`. The two
credential knobs are resolved in one place, with the raw `GCS_CREDENTIALS_JSON` document
winning over the `GOOGLE_APPLICATION_CREDENTIALS` path, because it is the credential itself
rather than a pointer to one.

`GcsStorageBackend` is checked against a hand-written fake rather than a real bucket, so
the suite needs no credentials and no network: every recorded `upload_from_file`,
`download_as_bytes`, `open`, `reload`, `delete` and `generate_signed_url` call is asserted
directly, including the wire key the prefix produces and the backend-relative key the row
records. The credential tests replace the two `service_account.Credentials` factories and
`storage.Client` itself with recorders, so the precedence between the two knobs is
assertable without a service-account key.

The same round trip runs against a real endpoint when `STORAGE_EMULATOR_HOST` (or
`GCS_EMULATOR_ENDPOINT`) names one, and is skipped otherwise.
"""

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

import app.gcs_storage as gcs_storage
from app.gcs_storage import (
    GCS_READ_CHUNK_SIZE,
    GCS_SIGNING_VERSION,
    GcsStorageBackend,
    build_gcs_client,
    resolve_gcs_credentials,
)
from app.media import (
    DEFAULT_GCS_MEDIA_PREFIX,
    GCS_STORAGE_BACKEND,
    MEDIA_URL_PREFIX,
    EmptyUploadError,
    ExactSizeMismatchError,
    LocalStorageBackend,
    MediaSettings,
    MediaTooLargeError,
    RequestStreamChunks,
    StorageHealth,
    StoredObject,
    UnsupportedMediaTypeError,
    build_storage_backend,
    integrity_report,
)
from app.presign import decode_upload_token
from app.schemas import MediaKind
from app.streaming import ByteRange

# (environment variable, the field it feeds, a value it round trips).
GCS_KNOBS: tuple[tuple[str, str, str], ...] = (
    ("GCS_BUCKET", "gcs_bucket", "bms-gcs-media"),
    ("GCS_PROJECT", "gcs_project", "bms-gcp-project"),
    ("GCS_CREDENTIALS_JSON", "gcs_credentials_json", '{"type": "service_account"}'),
    ("GOOGLE_APPLICATION_CREDENTIALS", "gcs_credentials_path", "/secrets/gcs.json"),
    ("GCS_MEDIA_PREFIX", "gcs_media_prefix", "prod/media"),
)


def _clear_gcs_env(monkeypatch) -> None:
    """Remove every GCS knob, so a test reads only what it sets itself."""
    for name, _, _ in GCS_KNOBS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _clear_client_cache() -> Iterator[None]:
    """Keep a client built for one test's settings from answering the next test's."""
    build_gcs_client.cache_clear()
    try:
        yield
    finally:
        build_gcs_client.cache_clear()


@pytest.mark.parametrize(("name", "field", "value"), GCS_KNOBS)
def test_gcs_settings_read_every_env_knob(monkeypatch, name, field, value) -> None:
    """Exactly one variable set: exactly its own field carries the value."""
    _clear_gcs_env(monkeypatch)
    monkeypatch.setenv(name, value)

    settings = MediaSettings.from_env()

    assert getattr(settings, field) == value


def test_gcs_defaults_when_nothing_is_configured(monkeypatch) -> None:
    _clear_gcs_env(monkeypatch)

    unset = MediaSettings.from_env()

    assert unset.gcs_bucket == ""
    assert unset.gcs_project == ""
    assert unset.gcs_credentials_json == ""
    assert unset.gcs_credentials_path == ""
    assert unset.gcs_media_prefix == DEFAULT_GCS_MEDIA_PREFIX == ""
    assert GCS_STORAGE_BACKEND == "gcs"

    # A blank variable reads as the default, exactly as a blank S3_* one already does.
    for name, _, _ in GCS_KNOBS:
        monkeypatch.setenv(name, "   ")

    blank = MediaSettings.from_env()

    assert blank.gcs_bucket == ""
    assert blank.gcs_project == ""
    assert blank.gcs_credentials_json == ""
    assert blank.gcs_credentials_path == ""
    assert blank.gcs_media_prefix == DEFAULT_GCS_MEDIA_PREFIX


def test_gcs_fields_default_on_a_plain_settings_object() -> None:
    """The five GCS fields are defaulted, so every existing constructor call still works."""
    settings = MediaSettings(
        storage_backend="local",
        upload_dir="/unused/local-media",
        max_image_bytes=1024,
        max_video_bytes=2048,
        service_token="test-service-token",
    )

    assert settings.gcs_bucket == ""
    assert settings.gcs_project == ""
    assert settings.gcs_credentials_json == ""
    assert settings.gcs_credentials_path == ""
    assert settings.gcs_media_prefix == DEFAULT_GCS_MEDIA_PREFIX


def test_gcs_media_prefix_is_normalised(monkeypatch) -> None:
    """A configured prefix never starts or ends with a slash, so keys join cleanly."""
    _clear_gcs_env(monkeypatch)
    for raw, expected in (
        ("/prod/media/", "prod/media"),
        ("prod/media", "prod/media"),
        ("/prod/media", "prod/media"),
        ("prod/media/", "prod/media"),
        ("", ""),
    ):
        monkeypatch.setenv("GCS_MEDIA_PREFIX", raw)

        assert MediaSettings.from_env().gcs_media_prefix == expected


AVATAR_BYTES = b"\x89PNG\r\n\x1a\navatar-bytes"
CONTENT_TYPE = "image/png"
MISSING_KEY = "avatars/no-such-object.png"
GCS_BUCKET = "bms-gcs-media"
GCS_PROJECT = "bms-gcp-project"


def _gcs_settings(**overrides: object) -> MediaSettings:
    """GCS-configured settings: one bucket, no prefix and no credentials unless asked."""
    values: dict[str, object] = {
        "storage_backend": "gcs",
        "upload_dir": "/unused/local-media",
        "max_image_bytes": 1024,
        "max_video_bytes": 2048,
        "service_token": "test-service-token",
        "presign_secret": "test-presign-secret",
        "presign_expires_seconds": 900,
        "gcs_bucket": GCS_BUCKET,
    }
    values.update(overrides)
    return MediaSettings(**values)  # type: ignore[arg-type]


class FakeSignedUrlError(Exception):
    """The error a keyless credential raises from `generate_signed_url`."""


class FakeNotFound(Exception):
    """A stand-in for google-api-core's NotFound, carrying the same 404 code.

    The backend recognises an absent object either through the library's own
    `NotFound` or through a code/status of 404, so the fake only has to carry the code
    and no test needs google-api-core's exception hierarchy.
    """

    def __init__(self, message: str = "404 Not Found") -> None:
        super().__init__(message)
        self.code = 404


class _FakeReader:
    """The file-like object `blob.open('rb')` answers with, slicing by the read size."""

    def __init__(self, payload: bytes, chunk_size: int = GCS_READ_CHUNK_SIZE) -> None:
        self._payload = payload
        self._chunk_size = chunk_size
        self._offset = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        limit = (
            self._chunk_size
            if size is None or size < 0
            else min(size, self._chunk_size)
        )
        chunk = self._payload[self._offset : self._offset + limit]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeBlob:
    """One object handle over the client's shared store, recording every call it gets.

    Every argument a real call would carry is recorded, so a test can assert the wire
    name (prefix included), the declared content type, the signed expiry and the method
    without a bucket or a network. A missing object raises a 404-coded error, which is
    what the backend's not-found handling reads.
    """

    def __init__(self, client: "FakeGcsClient", bucket: str, name: str) -> None:
        self._client = client
        self.bucket_name = bucket
        self.name = name
        self.size: int | None = None
        self.content_type: str | None = None

    def _payload(self) -> bytes:
        if self.name not in self._client.objects:
            raise FakeNotFound()
        return self._client.objects[self.name][0]

    def upload_from_file(
        self,
        file_obj: Any,
        content_type: str | None = None,
        rewind: bool = False,
    ) -> None:
        payload = file_obj.read()
        self._client.upload_calls.append(
            {
                "bucket": self.bucket_name,
                "name": self.name,
                "body": payload,
                "content_type": content_type,
                "rewind": rewind,
            }
        )
        self._client.objects[self.name] = (payload, content_type or "")
        self.size = len(payload)
        self.content_type = content_type

    def download_as_bytes(
        self, start: int | None = None, end: int | None = None
    ) -> bytes:
        payload = self._payload()
        self._client.download_calls.append(
            {
                "bucket": self.bucket_name,
                "name": self.name,
                "start": start,
                "end": end,
            }
        )
        first = 0 if start is None else start
        # `end` is inclusive, exactly as ByteRange and the real client's is.
        last = len(payload) - 1 if end is None else end
        return payload[first : last + 1]

    def open(
        self, mode: str = "rb", chunk_size: int = GCS_READ_CHUNK_SIZE
    ) -> _FakeReader:
        payload = self._payload()
        self._client.open_calls.append(
            {
                "bucket": self.bucket_name,
                "name": self.name,
                "mode": mode,
                "chunk_size": chunk_size,
            }
        )
        return _FakeReader(payload, chunk_size)

    def reload(self) -> None:
        self._client.reload_calls.append(
            {"bucket": self.bucket_name, "name": self.name}
        )
        if self.name not in self._client.objects:
            raise FakeNotFound()
        payload, content_type = self._client.objects[self.name]
        self.size = len(payload)
        self.content_type = content_type or None

    def delete(self) -> None:
        self._client.delete_calls.append(
            {"bucket": self.bucket_name, "name": self.name}
        )
        if self.name not in self._client.objects:
            raise FakeNotFound()
        del self._client.objects[self.name]

    def generate_signed_url(
        self,
        *,
        version: str,
        expiration: timedelta,
        method: str,
        content_type: str | None = None,
    ) -> str:
        if self._client.signing_error is not None:
            raise self._client.signing_error
        self._client.signed_calls.append(
            {
                "bucket": self.bucket_name,
                "name": self.name,
                "version": version,
                "expiration": expiration,
                "method": method,
                "content_type": content_type,
            }
        )
        seconds = int(expiration.total_seconds())
        return (
            f"https://storage.googleapis.com/{self.bucket_name}/{self.name}"
            f"?X-Goog-Expires={seconds}&X-Goog-Signature=fake"
        )


class FakeBucket:
    """A bucket handle over the client's shared store, recording its own probes."""

    def __init__(self, client: "FakeGcsClient", name: str) -> None:
        self._client = client
        self.name = name

    def blob(self, key: str) -> FakeBlob:
        self._client.blob_calls.append({"bucket": self.name, "name": key})
        return FakeBlob(self._client, self.name, key)

    def exists(self) -> bool:
        self._client.bucket_exists_calls.append(self.name)
        if self._client.bucket_probe_error is not None:
            raise self._client.bucket_probe_error
        return True


class FakeGcsClient:
    """A hand-written stand-in for google.cloud.storage.Client: in-memory, recording.

    `bucket(name)` and `get_bucket(name)` answer a `FakeBucket` over one shared object
    store, so a handle and the probe issued through it agree about the same bytes. A
    `bucket_probe_error` makes `bucket.exists()` raise, which is how a test drives the
    unreachable health state, and a `signing_error` makes every
    `generate_signed_url` raise, which is how a test drives the keyless-credential
    fallback.
    """

    def __init__(
        self,
        *,
        bucket_probe_error: BaseException | None = None,
        signing_error: BaseException | None = None,
    ) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.bucket_probe_error = bucket_probe_error
        self.signing_error = signing_error
        self.bucket_calls: list[str] = []
        self.get_bucket_calls: list[str] = []
        self.bucket_exists_calls: list[str] = []
        self.blob_calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []
        self.open_calls: list[dict[str, Any]] = []
        self.download_calls: list[dict[str, Any]] = []
        self.reload_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.signed_calls: list[dict[str, Any]] = []

    def bucket(self, name: str) -> FakeBucket:
        self.bucket_calls.append(name)
        return FakeBucket(self, name)

    def get_bucket(self, name: str) -> FakeBucket:
        self.get_bucket_calls.append(name)
        return FakeBucket(self, name)


@pytest.fixture()
def fake_client() -> FakeGcsClient:
    """A fresh recording client, so every call one test makes is its own."""
    return FakeGcsClient()


def _fake_backend(fake_client: FakeGcsClient, **overrides: object) -> GcsStorageBackend:
    """A backend over the injected fake, never over the real client builder."""
    return GcsStorageBackend(_gcs_settings(**overrides), client=fake_client)


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


async def _body(payload: bytes, chunk_size: int = 4) -> AsyncIterator[bytes]:
    """The chunks a server would hand storage for this body."""
    for start in range(0, len(payload), chunk_size):
        yield payload[start : start + chunk_size]


def _save(
    backend: GcsStorageBackend,
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
    backend: GcsStorageBackend,
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


def test_backend_exposes_the_whole_storage_seam(fake_client) -> None:
    """Every member the `StorageBackend` protocol declares is present and callable."""
    backend = _fake_backend(fake_client)

    for member in (
        "save",
        "save_exact",
        "public_url",
        "open_stream",
        "read_bytes",
        "delete",
        "exists",
        "head",
        "presigned_upload",
        "health",
    ):
        assert callable(getattr(backend, member))


def test_save_uploads_the_bytes_with_the_declared_type(fake_client) -> None:
    backend = _fake_backend(fake_client)

    stored = _save(backend, AVATAR_BYTES)

    assert stored.size == len(AVATAR_BYTES)
    assert stored.sha256 == hashlib.sha256(AVATAR_BYTES).hexdigest()
    assert stored.key == f"avatars/{stored.sha256}.png"

    assert len(fake_client.upload_calls) == 1
    upload = fake_client.upload_calls[0]
    assert upload["bucket"] == GCS_BUCKET
    assert upload["name"] == stored.key
    assert upload["body"] == AVATAR_BYTES
    assert upload["content_type"] == CONTENT_TYPE
    # The spool is rewound, so the whole body is uploaded from byte zero.
    assert upload["rewind"] is True


def test_save_refuses_an_empty_or_oversized_body(fake_client) -> None:
    backend = _fake_backend(fake_client)

    with pytest.raises(EmptyUploadError):
        _save(backend, b"")

    with pytest.raises(MediaTooLargeError) as caught:
        _save(backend, b"x" * 9, max_bytes=8)

    assert caught.value.limit == 8
    # A refused upload never reaches the bucket.
    assert fake_client.upload_calls == []


@pytest.mark.parametrize("delta", [-1, 1])
def test_save_exact_refuses_a_body_off_the_declared_size(
    fake_client, delta: int
) -> None:
    backend = _fake_backend(fake_client)

    stored = _save_exact(backend, AVATAR_BYTES)

    assert stored.size == len(AVATAR_BYTES)
    assert stored.sha256 == hashlib.sha256(AVATAR_BYTES).hexdigest()
    assert fake_client.upload_calls[0]["name"] == stored.key

    fake_client.upload_calls.clear()
    expected = len(AVATAR_BYTES) + delta

    with pytest.raises(ExactSizeMismatchError) as caught:
        _save_exact(backend, AVATAR_BYTES, expected_size=expected)

    assert caught.value.expected == expected
    assert caught.value.actual == len(AVATAR_BYTES)
    # Neither a short nor a long body is ever uploaded.
    assert fake_client.upload_calls == []


@pytest.mark.parametrize("prefix", ["prod/media", ""])
def test_every_key_is_under_the_media_prefix(prefix: str) -> None:
    """Every wire key carries the configured prefix once, and nothing else does."""
    fake_client = FakeGcsClient()
    backend = _fake_backend(fake_client, gcs_media_prefix=prefix)

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
        *[call["name"] for call in fake_client.upload_calls],
        *[call["name"] for call in fake_client.open_calls],
        *[call["name"] for call in fake_client.download_calls],
        *[call["name"] for call in fake_client.reload_calls],
        *[call["name"] for call in fake_client.delete_calls],
        *[call["name"] for call in fake_client.signed_calls],
    }

    assert keys == {wire(stored.key), wire(presigned.key)}
    assert all("//" not in key and not key.startswith("/") for key in keys)
    # The backend-relative keys the routes record never carry the prefix.
    assert stored.key == f"avatars/{stored.sha256}.png"
    assert presigned.key == "avatars/7.png"


def test_open_stream_honours_a_byte_range(fake_client) -> None:
    backend = _fake_backend(fake_client)
    payload = bytes(range(10))
    key = _save_exact(backend, payload).key

    assert b"".join(backend.open_stream(key)) == payload
    # No range streams the blob out in bounded chunks through one open handle.
    assert fake_client.open_calls[-1]["mode"] == "rb"
    assert fake_client.open_calls[-1]["chunk_size"] == GCS_READ_CHUNK_SIZE
    assert fake_client.download_calls == []

    assert b"".join(backend.open_stream(key, ByteRange(start=2, end=4))) == payload[2:5]
    # `start`/`end` reach the client inclusive, exactly as ByteRange spells them.
    assert fake_client.download_calls[-1]["start"] == 2
    assert fake_client.download_calls[-1]["end"] == 4

    large = b"y" * (GCS_READ_CHUNK_SIZE * 2 + 7)
    large_key = _save(
        backend, large, max_bytes=len(large), chunk_size=GCS_READ_CHUNK_SIZE
    ).key

    chunks = list(backend.open_stream(large_key))

    assert b"".join(chunks) == large
    assert len(chunks) == 3
    assert all(len(chunk) <= GCS_READ_CHUNK_SIZE for chunk in chunks)


def test_read_head_exists_and_delete(fake_client) -> None:
    backend = _fake_backend(fake_client)
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


def test_integrity_report_follows_the_stored_bytes(fake_client) -> None:
    backend = _fake_backend(fake_client)
    key = _save(backend, AVATAR_BYTES).key
    digest = hashlib.sha256(AVATAR_BYTES).hexdigest()

    verified = integrity_report(
        storage=backend,
        storage_key=key,
        expected_sha256=digest,
        expected_size=len(AVATAR_BYTES),
        algorithm="sha256",
    )

    assert verified.verified is True
    assert verified.recomputed_sha256 == digest
    assert verified.recomputed_size == len(AVATAR_BYTES)

    backend.delete(key)

    missing = integrity_report(
        storage=backend,
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


def test_presigned_get_and_put_use_the_configured_expiry(fake_client) -> None:
    settings = _gcs_settings()
    backend = GcsStorageBackend(settings, client=fake_client)
    expiry = timedelta(seconds=settings.presign_expires_seconds)

    url = backend.public_url("avatars/x.png")

    get_call = fake_client.signed_calls[0]
    assert get_call["method"] == "GET"
    assert get_call["version"] == GCS_SIGNING_VERSION == "v4"
    assert get_call["expiration"] == expiry
    assert (get_call["bucket"], get_call["name"]) == (
        GCS_BUCKET,
        "avatars/x.png",
    )
    assert get_call["content_type"] is None
    assert url.startswith(
        f"https://storage.googleapis.com/{GCS_BUCKET}/avatars/x.png?"
    )
    assert "X-Goog-Expires=900" in url

    presigned = backend.presigned_upload(
        asset_id=7,
        kind=MediaKind.AVATAR,
        size=len(AVATAR_BYTES),
        content_type=CONTENT_TYPE,
    )

    put_call = fake_client.signed_calls[1]
    assert put_call["method"] == "PUT"
    assert put_call["expiration"] == expiry
    assert (put_call["bucket"], put_call["name"]) == (
        GCS_BUCKET,
        "avatars/7.png",
    )
    assert put_call["content_type"] == CONTENT_TYPE

    assert presigned.method == "PUT"
    assert presigned.key == "avatars/7.png"
    assert presigned.headers == {"Content-Type": CONTENT_TYPE}
    assert "X-Goog-Expires=900" in presigned.url


def test_presigned_upload_rejects_an_unsupported_content_type(fake_client) -> None:
    backend = _fake_backend(fake_client)

    with pytest.raises(UnsupportedMediaTypeError) as caught:
        backend.presigned_upload(
            asset_id=7,
            kind=MediaKind.AVATAR,
            size=len(AVATAR_BYTES),
            content_type="image/gif",
        )

    assert caught.value.content_type == "image/gif"
    assert fake_client.signed_calls == []


def test_signing_failure_falls_back_to_the_token_route() -> None:
    """A keyless identity cannot sign, so the token-signed route answers instead."""
    fake_client = FakeGcsClient(signing_error=FakeSignedUrlError("no private key"))
    settings = _gcs_settings(gcs_media_prefix="prod")
    backend = GcsStorageBackend(settings, client=fake_client)

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
    # The local shape: the bytes-derived key means there is no destination to record.
    assert presigned.key == ""

    grant = decode_upload_token(settings, token=presigned.url[len(prefix) :])

    assert grant.asset_id == 7
    assert grant.kind is MediaKind.AVATAR
    assert grant.size == len(AVATAR_BYTES)
    assert grant.content_type == CONTENT_TYPE

    # The read url falls back the same way rather than raising.
    assert backend.public_url("avatars/x.png") == f"{MEDIA_URL_PREFIX}/avatars/x.png"
    assert fake_client.signed_calls == []


def test_health_reports_reachable_and_unreachable() -> None:
    healthy = _fake_backend(FakeGcsClient())

    assert healthy.health() == StorageHealth(backend="gcs", reachable=True)

    denied = _fake_backend(
        FakeGcsClient(bucket_probe_error=FakeSignedUrlError("denied"))
    )

    assert denied.health() == StorageHealth(backend="gcs", reachable=False)

    # A bucket that is simply gone is reported down, never raised out of the probe.
    broken = _fake_backend(
        FakeGcsClient(bucket_probe_error=RuntimeError("endpoint down"))
    )

    assert broken.health() == StorageHealth(backend="gcs", reachable=False)

    probe_free = FakeGcsClient()
    unconfigured = GcsStorageBackend(
        replace(_gcs_settings(), gcs_bucket=""), client=probe_free
    )

    assert unconfigured.health() == StorageHealth(backend="gcs", reachable=False)
    # A blank bucket is decided without a call to the client at all.
    assert probe_free.bucket_calls == []


def test_gcs_is_selected_by_media_storage_backend(monkeypatch) -> None:
    """MEDIA_STORAGE_BACKEND=gcs with a bucket configured selects the GCS backend."""
    fake_client = FakeGcsClient()
    monkeypatch.setattr(gcs_storage, "build_gcs_client", lambda settings: fake_client)
    settings = _gcs_settings()

    backend = build_storage_backend(settings)

    assert isinstance(backend, GcsStorageBackend)
    assert backend.settings is settings
    assert backend.client is fake_client


def test_gcs_without_a_bucket_falls_back_to_local(tmp_path, caplog) -> None:
    """gcs with no bucket is a configuration mistake, not a usable backend."""
    configured = replace(_gcs_settings(upload_dir=str(tmp_path / "media")), gcs_bucket="")

    with caplog.at_level(logging.WARNING, logger="app.media"):
        backend = build_storage_backend(configured)

    assert isinstance(backend, LocalStorageBackend)
    assert backend.settings is configured
    assert any("GCS_BUCKET" in record.getMessage() for record in caplog.records)


def test_gcs_with_unusable_credentials_falls_back_to_local(
    monkeypatch, tmp_path, caplog
) -> None:
    """A client whose credentials cannot be resolved falls back rather than raising."""
    document = "{not-a-service-account-document"

    def _unusable(settings: MediaSettings) -> Any:
        raise ValueError(f"GCS_CREDENTIALS_JSON is not a document: {document!r}")

    monkeypatch.setattr(gcs_storage, "build_gcs_client", _unusable)
    configured = _gcs_settings(
        upload_dir=str(tmp_path / "media"), gcs_credentials_json=document
    )

    with caplog.at_level(logging.WARNING, logger="app.media"):
        backend = build_storage_backend(configured)

    assert isinstance(backend, LocalStorageBackend)
    assert backend.settings is configured
    assert any("unusable" in record.getMessage() for record in caplog.records)


class _RecordingCredentials:
    """Stand-in for the library's credentials, tagged with where it was built from."""

    def __init__(self, source: str) -> None:
        self.source = source


@pytest.fixture()
def credential_recorders(monkeypatch):
    """Replace both credential factories and the client factory with recorders."""
    info_calls: list[dict[str, object]] = []
    file_calls: list[str] = []
    client_options: list[dict[str, object]] = []

    def _from_info(info: dict[str, object]) -> _RecordingCredentials:
        info_calls.append(info)
        return _RecordingCredentials("json")

    def _from_file(path: str) -> _RecordingCredentials:
        file_calls.append(path)
        return _RecordingCredentials("file")

    def _client(**options: object) -> object:
        client_options.append(options)
        return object()

    monkeypatch.setattr(
        gcs_storage.service_account.Credentials,
        "from_service_account_info",
        staticmethod(_from_info),
    )
    monkeypatch.setattr(
        gcs_storage.service_account.Credentials,
        "from_service_account_file",
        staticmethod(_from_file),
    )
    monkeypatch.setattr(gcs_storage.storage, "Client", _client)

    return info_calls, file_calls, client_options


def test_credentials_json_wins_over_the_path(credential_recorders) -> None:
    """The raw document is the credential itself, so it wins over a pointer to one."""
    info_calls, file_calls, client_options = credential_recorders
    document = '{"type": "service_account", "project_id": "bms"}'
    settings = _gcs_settings(
        gcs_credentials_json=document,
        gcs_credentials_path="/secrets/gcs.json",
        gcs_project=GCS_PROJECT,
    )

    credentials = resolve_gcs_credentials(settings)

    assert isinstance(credentials, _RecordingCredentials)
    assert credentials.source == "json"
    assert info_calls == [json.loads(document)]
    # GOOGLE_APPLICATION_CREDENTIALS is never read while the document is configured.
    assert file_calls == []

    build_gcs_client(settings)

    assert len(client_options) == 1
    options = client_options[0]
    assert options["project"] == GCS_PROJECT
    assert isinstance(options["credentials"], _RecordingCredentials)
    assert options["credentials"].source == "json"


def test_credentials_path_is_used_when_only_it_is_set(credential_recorders) -> None:
    info_calls, file_calls, client_options = credential_recorders
    settings = _gcs_settings(gcs_credentials_path="/secrets/gcs.json")

    credentials = resolve_gcs_credentials(settings)

    assert isinstance(credentials, _RecordingCredentials)
    assert credentials.source == "file"
    assert file_calls == ["/secrets/gcs.json"]
    assert info_calls == []

    build_gcs_client(settings)

    options = client_options[0]
    # A blank project is left to whatever the credentials themselves name.
    assert "project" not in options
    assert options["credentials"].source == "file"


def test_no_credentials_leaves_the_default_lookup_to_the_library(
    credential_recorders,
) -> None:
    info_calls, file_calls, client_options = credential_recorders
    settings = _gcs_settings()

    assert resolve_gcs_credentials(settings) is None

    built = build_gcs_client(settings)

    assert isinstance(built, object)
    assert info_calls == []
    assert file_calls == []
    # Neither knob set: neither credentials nor project is passed to the library.
    assert client_options == [{}]


STORAGE_EMULATOR_HOST: str | None = os.getenv(
    "STORAGE_EMULATOR_HOST"
) or os.getenv("GCS_EMULATOR_ENDPOINT")


@pytest.mark.skipif(
    not STORAGE_EMULATOR_HOST,
    reason="set STORAGE_EMULATOR_HOST to a fake-gcs-server endpoint to run",
)
def test_emulator_round_trip(monkeypatch) -> None:
    """The same round trip against a real endpoint: fake-gcs-server or GCS itself."""
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    monkeypatch.setenv("STORAGE_EMULATOR_HOST", str(STORAGE_EMULATOR_HOST))
    settings = _gcs_settings(
        gcs_bucket=os.getenv("GCS_EMULATOR_BUCKET", GCS_BUCKET),
        gcs_media_prefix="integration",
    )
    # Anonymous credentials: an emulator needs no service-account key to be reached.
    client = storage.Client(
        project=os.getenv("GCS_EMULATOR_PROJECT", GCS_PROJECT),
        credentials=AnonymousCredentials(),
    )
    backend = GcsStorageBackend(settings, client=client)
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
        presigned = backend.presigned_upload(
            asset_id=7,
            kind=MediaKind.AVATAR,
            size=len(AVATAR_BYTES),
            content_type=CONTENT_TYPE,
        )
        assert presigned.key == "avatars/7.png"
        assert backend.health().reachable is True
    finally:
        backend.delete(stored.key)

    assert backend.exists(stored.key) is False
