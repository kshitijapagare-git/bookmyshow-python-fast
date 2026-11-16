"""The MinIO backing for media storage: the S3 backend pointed at a MinIO endpoint.

`MinioStorageBackend` is the S3 backend re-aimed at MinIO, so the whole storage seam --
put, get, stream, delete, exists, head, the presigned urls and the two save paths -- is
exercised here against a hand-written fake client rather than moto, boto3 or a running
container: every recorded `put_object`, `get_object`, `head_object`, `delete_object`,
`head_bucket`, `create_bucket` and presigned call is asserted directly. The tests build
`MinioStorageBackend(settings, client=FakeMinioClient())` for exactly that reason.

Three things are MinIO's own and are checked as such: the client is built with the
configured endpoint (a bare `host:port` gaining the scheme `MINIO_SECURE` names), the
credentials and path-style addressing -- asserted through a stubbed `boto3`, exactly as
tests/test_s3_storage.py stubs it; the bucket is created lazily, once, and only when
`MINIO_CREATE_BUCKET` asks for it; and the health report names the endpoint and the
bucket. `MEDIA_STORAGE_BACKEND=minio` selects the backend, and a blank `MINIO_BUCKET` or
`MINIO_ENDPOINT` falls back to local with a warning, exactly as an s3 selection with no
bucket does.

The same round trip runs against a real MinIO when `MINIO_INTEGRATION_ENDPOINT` names
one, and is skipped otherwise.
"""

import asyncio
import hashlib
import logging
import os
import sys
import types
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from typing import Any

import pytest

from app.media import (
    LocalStorageBackend,
    MediaSettings,
    RequestStreamChunks,
    StorageHealth,
    StoredObject,
    build_storage_backend,
)
from app.minio_storage import (
    MINIO_ADDRESSING_STYLE,
    MinioStorageBackend,
    build_minio_client,
    minio_endpoint_url,
    minio_settings,
)
from app.s3_storage import (
    S3_PRIVATE_ACL,
    S3_READ_CHUNK_SIZE,
    S3StorageBackend,
)
from app.schemas import MediaKind
from app.streaming import ByteRange

MINIO_ENDPOINT = "localhost:9000"
# The endpoint as a url: a bare `host:port` with MINIO_SECURE off gains http.
MINIO_ENDPOINT_URL = f"http://{MINIO_ENDPOINT}"
MINIO_BUCKET = "bms-minio-media"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"
AVATAR_BYTES = b"\x89PNG\r\n\x1a\navatar-bytes"
CONTENT_TYPE = "image/png"
MISSING_KEY = "avatars/no-such-object.png"


@pytest.fixture(autouse=True)
def _clear_client_cache() -> Iterator[None]:
    """Keep a client built for one test's settings from answering the next test's."""
    build_minio_client.cache_clear()
    try:
        yield
    finally:
        build_minio_client.cache_clear()


def _minio_settings(**overrides: object) -> MediaSettings:
    """MinIO-configured settings: one bucket on an http endpoint, no prefix by default."""
    values: dict[str, object] = {
        "storage_backend": "minio",
        "upload_dir": "/unused/local-media",
        "max_image_bytes": 1024,
        "max_video_bytes": 2048,
        "service_token": "test-service-token",
        "presign_secret": "test-presign-secret",
        "presign_expires_seconds": 900,
        "minio_endpoint": MINIO_ENDPOINT,
        "minio_access_key": MINIO_ACCESS_KEY,
        "minio_secret_key": MINIO_SECRET_KEY,
        "minio_bucket": MINIO_BUCKET,
        "minio_secure": False,
    }
    values.update(overrides)
    return MediaSettings(**values)  # type: ignore[arg-type]


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

    Every argument a real call would carry is recorded, so a test can assert the wire
    key (prefix included), the bucket, the private ACL and the presigned expiry without
    a bucket or a network. The bucket set decides what `head_bucket` reports: a bucket
    that is not there raises the same `response`-shaped 404 botocore raises, which is
    what the backend's not-found handling and its lazy creation read. An injected
    `head_bucket_error` or `create_bucket_error` drives the two failure paths.
    """

    def __init__(
        self,
        *,
        buckets: tuple[str, ...] = (MINIO_BUCKET,),
        head_bucket_error: BaseException | None = None,
        create_bucket_error: BaseException | None = None,
    ) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.buckets: set[str] = set(buckets)
        self.head_bucket_error = head_bucket_error
        self.create_bucket_error = create_bucket_error
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.head_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.head_bucket_calls: list[dict[str, Any]] = []
        self.create_bucket_calls: list[dict[str, Any]] = []
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

    def create_bucket(self, *, Bucket: str) -> dict[str, Any]:
        self.create_bucket_calls.append({"Bucket": Bucket})
        if self.create_bucket_error is not None:
            raise self.create_bucket_error
        self.buckets.add(Bucket)
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
    backend: MinioStorageBackend,
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
    backend: MinioStorageBackend,
    payload: bytes,
    *,
    content_type: str = CONTENT_TYPE,
) -> StoredObject:
    """Save one avatar through the exact-size path a direct upload uses."""
    return asyncio.run(
        backend.save_exact(
            kind=MediaKind.AVATAR,
            content_type=content_type,
            upload=RequestStreamChunks(_body(payload)),
            expected_size=len(payload),
        )
    )


@pytest.mark.parametrize("prefix", ["prod/media", ""])
def test_minio_round_trip_put_get_stream_delete_exists(prefix: str) -> None:
    """Put, get, stream, head, exists and delete against the configured bucket."""
    settings = _minio_settings(minio_media_prefix=prefix)
    fake = FakeMinioClient()
    backend = MinioStorageBackend(settings, client=fake)

    stored = _save(backend, AVATAR_BYTES)

    # The whole seam is the S3 backend's: only the client, the bucket and health differ.
    assert isinstance(backend, S3StorageBackend)
    assert stored.size == len(AVATAR_BYTES)
    assert stored.sha256 == hashlib.sha256(AVATAR_BYTES).hexdigest()
    # The row records the backend-relative key; the prefix is added only on the wire.
    assert stored.key == f"avatars/{stored.sha256}.png"

    def wire(key: str) -> str:
        return f"{prefix}/{key}" if prefix else key

    put = fake.put_calls[0]
    assert put["Bucket"] == MINIO_BUCKET
    assert put["Key"] == wire(stored.key)
    assert put["Body"] == AVATAR_BYTES
    assert put["ContentType"] == CONTENT_TYPE
    # Private by default: no public ACL value is ever sent.
    assert put["ACL"] == S3_PRIVATE_ACL == "private"
    # No SSE knob is configured, so nothing is sent.
    assert put["ServerSideEncryption"] is None

    assert backend.exists(stored.key) is True
    assert backend.read_bytes(stored.key) == AVATAR_BYTES

    head = backend.head(stored.key)

    assert head is not None
    assert head.size == len(AVATAR_BYTES)
    assert head.content_type == CONTENT_TYPE

    payload = bytes(range(10))
    ranged_key = _save_exact(backend, payload).key

    assert b"".join(backend.open_stream(ranged_key)) == payload
    # No range means no Range header: the whole object comes back.
    assert fake.get_calls[-1]["Range"] is None

    assert (
        b"".join(backend.open_stream(ranged_key, ByteRange(start=2, end=4)))
        == payload[2:5]
    )
    assert fake.get_calls[-1]["Range"] == "bytes=2-4"

    large = b"y" * (S3_READ_CHUNK_SIZE * 2 + 7)
    large_key = _save(
        backend, large, max_bytes=len(large), chunk_size=S3_READ_CHUNK_SIZE
    ).key

    chunks = list(backend.open_stream(large_key))

    assert b"".join(chunks) == large
    assert len(chunks) == 3
    assert all(len(chunk) <= S3_READ_CHUNK_SIZE for chunk in chunks)

    assert backend.exists(MISSING_KEY) is False
    assert backend.head(MISSING_KEY) is None

    keys = {
        *[call["Key"] for call in fake.put_calls],
        *[call["Key"] for call in fake.get_calls],
        *[call["Key"] for call in fake.head_calls],
    }

    assert keys == {wire(stored.key), wire(ranged_key), wire(large_key)}
    assert all("//" not in key and not key.startswith("/") for key in keys)
    # Every object operation addressed the configured bucket.
    assert {call["Bucket"] for call in fake.put_calls} == {MINIO_BUCKET}
    assert {call["Bucket"] for call in fake.get_calls} == {MINIO_BUCKET}

    backend.delete(stored.key)

    assert fake.delete_calls == [{"Bucket": MINIO_BUCKET, "Key": wire(stored.key)}]
    assert backend.exists(stored.key) is False
    # Deleting an object that is already gone is not an error.
    backend.delete(stored.key)


def test_minio_presigned_put_and_get_respect_expiry() -> None:
    """Both signed urls come from the MinIO endpoint and carry the configured expiry."""
    settings = _minio_settings()
    fake = FakeMinioClient()
    backend = MinioStorageBackend(settings, client=fake)

    url = backend.public_url("avatars/x.png")

    get_call = fake.presigned_calls[0]
    assert get_call["method"] == "get_object"
    assert get_call["expires_in"] == settings.presign_expires_seconds == 900
    assert get_call["params"] == {"Bucket": MINIO_BUCKET, "Key": "avatars/x.png"}
    # The url is the MinIO endpoint's own signed GET, not a cloud one.
    assert url.startswith(f"{MINIO_ENDPOINT_URL}/{MINIO_BUCKET}/avatars/x.png?")
    assert "X-Amz-Expires=900" in url

    presigned = backend.presigned_upload(
        asset_id=7,
        kind=MediaKind.AVATAR,
        size=len(AVATAR_BYTES),
        content_type=CONTENT_TYPE,
    )

    put_call = fake.presigned_calls[1]
    assert put_call["method"] == "put_object"
    assert put_call["expires_in"] == 900
    assert put_call["params"] == {
        "Bucket": MINIO_BUCKET,
        "Key": "avatars/7.png",
        "ContentType": CONTENT_TYPE,
    }
    # A presigned PUT never carries a public ACL, and no SSE knob is configured.
    assert "ACL" not in put_call["params"]
    assert "ServerSideEncryption" not in put_call["params"]

    assert presigned.method == "PUT"
    assert presigned.key == "avatars/7.png"
    assert presigned.headers == {"Content-Type": CONTENT_TYPE}
    assert presigned.url.startswith(
        f"{MINIO_ENDPOINT_URL}/{MINIO_BUCKET}/avatars/7.png?"
    )
    assert "X-Amz-Expires=900" in presigned.url


def test_minio_creates_the_bucket_once_when_configured() -> None:
    """`MINIO_CREATE_BUCKET` heads then creates once, and never when it is off."""
    settings = _minio_settings(minio_create_bucket=True)
    missing = FakeMinioClient(buckets=())
    backend = MinioStorageBackend(settings, client=missing)

    _save(backend, AVATAR_BYTES)
    _save(backend, b"\x89PNG\r\n\x1a\nsecond-avatar-bytes")

    # One probe found the bucket missing, one creation answered it, and the second
    # operation re-used that answer instead of probing again.
    assert missing.head_bucket_calls == [{"Bucket": MINIO_BUCKET}]
    assert missing.create_bucket_calls == [{"Bucket": MINIO_BUCKET}]
    assert [call["Bucket"] for call in missing.put_calls] == [
        MINIO_BUCKET,
        MINIO_BUCKET,
    ]

    # Creation off: nothing is created and no object operation probes the bucket.
    off = FakeMinioClient()
    plain = MinioStorageBackend(_minio_settings(minio_create_bucket=False), client=off)

    _save(plain, AVATAR_BYTES)

    assert off.create_bucket_calls == []
    assert off.head_bucket_calls == []

    # A head that fails for any other reason surfaces as the operation's own error.
    denied = FakeMinioClient(head_bucket_error=_ClientError("403", 403))
    refusing = MinioStorageBackend(settings, client=denied)

    with pytest.raises(_ClientError):
        _save(refusing, AVATAR_BYTES)

    assert denied.put_calls == []
    assert denied.create_bucket_calls == []

    # A creation that fails propagates instead of being swallowed.
    broken = FakeMinioClient(
        buckets=(), create_bucket_error=RuntimeError("bucket denied")
    )
    failing = MinioStorageBackend(settings, client=broken)

    with pytest.raises(RuntimeError):
        _save(failing, AVATAR_BYTES)

    assert broken.put_calls == []


def _stub_boto3(monkeypatch) -> list[dict[str, object]]:
    """Replace boto3 and botocore.config with recorders, so no SDK is needed."""
    built: list[dict[str, object]] = []

    class _FakeBoto3:
        @staticmethod
        def client(name: str, **options: object) -> object:
            built.append({"name": name, "options": options})
            return object()

    class _FakeConfig:
        def __init__(self, **options: object) -> None:
            self.options = options

    botocore = types.ModuleType("botocore")
    config = types.ModuleType("botocore.config")
    config.Config = _FakeConfig  # type: ignore[attr-defined]
    botocore.config = config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", config)
    return built


def test_minio_client_uses_the_endpoint_and_path_style(monkeypatch) -> None:
    """The client is built for the MinIO endpoint, with the credentials and path-style."""
    built = _stub_boto3(monkeypatch)
    settings = _minio_settings(minio_secure=False)

    MinioStorageBackend(settings).client

    assert built[0]["name"] == "s3"
    options = built[0]["options"]
    assert isinstance(options, dict)
    assert options["endpoint_url"] == "http://localhost:9000"
    assert options["aws_access_key_id"] == MINIO_ACCESS_KEY
    assert options["aws_secret_access_key"] == MINIO_SECRET_KEY
    assert options["region_name"] == settings.s3_region
    # Path-style is what keeps object requests and signed urls on the endpoint itself.
    config = options["config"]
    assert MINIO_ADDRESSING_STYLE == "path"
    assert getattr(config, "options") == {
        "s3": {"addressing_style": MINIO_ADDRESSING_STYLE}
    }

    build_minio_client.cache_clear()
    built.clear()

    MinioStorageBackend(replace(settings, minio_secure=True)).client

    secure_options = built[0]["options"]
    assert isinstance(secure_options, dict)
    assert secure_options["endpoint_url"] == "https://localhost:9000"

    build_minio_client.cache_clear()
    built.clear()

    explicit = replace(settings, minio_endpoint="http://minio.internal:9001")
    MinioStorageBackend(explicit).client

    explicit_options = built[0]["options"]
    assert isinstance(explicit_options, dict)
    # An endpoint that already names its scheme is kept exactly as configured.
    assert explicit_options["endpoint_url"] == "http://minio.internal:9001"

    # A blank endpoint never falls back to boto3's default, which is AWS S3 itself.
    with pytest.raises(ValueError):
        build_minio_client(minio_settings(replace(settings, minio_endpoint="")))

    assert (
        minio_endpoint_url(replace(settings, minio_endpoint=MINIO_ENDPOINT))
        == MINIO_ENDPOINT_URL
    )
    assert (
        minio_endpoint_url(
            replace(settings, minio_endpoint=MINIO_ENDPOINT, minio_secure=True)
        )
        == f"https://{MINIO_ENDPOINT}"
    )
    assert minio_endpoint_url(replace(settings, minio_endpoint="")) == ""


def test_minio_health_reports_endpoint_and_bucket() -> None:
    """The probe names where the backend is pointed, and a failure only reports down."""
    settings = _minio_settings()
    healthy_fake = FakeMinioClient()
    healthy = MinioStorageBackend(settings, client=healthy_fake)

    assert healthy.health() == StorageHealth(
        backend="minio",
        reachable=True,
        bucket=MINIO_BUCKET,
        endpoint=MINIO_ENDPOINT_URL,
    )
    # Reachability is a real round trip to the configured bucket, not a guess.
    assert healthy_fake.head_bucket_calls == [{"Bucket": MINIO_BUCKET}]

    denied = MinioStorageBackend(
        settings, client=FakeMinioClient(head_bucket_error=_ClientError("403", 403))
    )

    assert denied.health() == StorageHealth(
        backend="minio",
        reachable=False,
        bucket=MINIO_BUCKET,
        endpoint=MINIO_ENDPOINT_URL,
    )

    # An endpoint that is simply gone is reported down, never raised out of the probe.
    broken = MinioStorageBackend(
        settings,
        client=FakeMinioClient(head_bucket_error=RuntimeError("endpoint down")),
    )

    assert broken.health() == StorageHealth(
        backend="minio",
        reachable=False,
        bucket=MINIO_BUCKET,
        endpoint=MINIO_ENDPOINT_URL,
    )

    probe_free = FakeMinioClient()
    unconfigured = MinioStorageBackend(
        _minio_settings(minio_bucket=""), client=probe_free
    )

    assert unconfigured.health() == StorageHealth(
        backend="minio", reachable=False, bucket="", endpoint=MINIO_ENDPOINT_URL
    )
    # A blank bucket is decided without a call to the client at all.
    assert probe_free.head_bucket_calls == []


def test_minio_backend_selection_and_fallbacks(caplog) -> None:
    """`minio` selects the MinIO backend, and an unusable selection warns and falls back."""
    settings = _minio_settings()

    with caplog.at_level(logging.WARNING, logger="app.media"):
        selected = build_storage_backend(settings)

    assert isinstance(selected, MinioStorageBackend)
    # The MINIO_* knobs are mapped onto the inherited S3 ones, which is what lets the
    # whole storage seam be inherited unchanged.
    assert selected.settings.s3_bucket == MINIO_BUCKET
    assert selected.settings.s3_endpoint_url == MINIO_ENDPOINT_URL
    assert selected.settings.s3_access_key_id == MINIO_ACCESS_KEY
    assert selected.settings.s3_secret_access_key == MINIO_SECRET_KEY
    assert selected.settings.s3_media_prefix == settings.minio_media_prefix
    # `minio` is a known backend name, not the unknown one that falls back.
    assert not any(
        "unknown MEDIA_STORAGE_BACKEND" in record.getMessage()
        for record in caplog.records
    )

    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="app.media"):
        without_bucket = build_storage_backend(_minio_settings(minio_bucket=""))

    assert isinstance(without_bucket, LocalStorageBackend)
    assert any("MINIO_BUCKET" in record.getMessage() for record in caplog.records)

    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="app.media"):
        without_endpoint = build_storage_backend(_minio_settings(minio_endpoint=""))

    assert isinstance(without_endpoint, LocalStorageBackend)
    assert any("MINIO_ENDPOINT" in record.getMessage() for record in caplog.records)
    assert not any(
        "unknown MEDIA_STORAGE_BACKEND" in record.getMessage()
        for record in caplog.records
    )


MINIO_INTEGRATION_ENDPOINT = os.getenv("MINIO_INTEGRATION_ENDPOINT")


@pytest.mark.skipif(
    not MINIO_INTEGRATION_ENDPOINT,
    reason="set MINIO_INTEGRATION_ENDPOINT to a running MinIO endpoint to run",
)
def test_minio_container_round_trip() -> None:
    """The same round trip against a real MinIO, with the bucket created for the test."""
    endpoint = str(MINIO_INTEGRATION_ENDPOINT)
    settings = _minio_settings(
        minio_endpoint=endpoint,
        minio_access_key=os.getenv("MINIO_INTEGRATION_ACCESS_KEY", MINIO_ACCESS_KEY),
        minio_secret_key=os.getenv("MINIO_INTEGRATION_SECRET_KEY", MINIO_SECRET_KEY),
        minio_bucket=os.getenv("MINIO_INTEGRATION_BUCKET", MINIO_BUCKET),
        minio_secure=endpoint.lower().startswith("https://"),
        minio_create_bucket=True,
        minio_media_prefix="integration",
    )
    base_url = minio_endpoint_url(settings)
    backend = MinioStorageBackend(settings)
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
        # Both signed urls are the MinIO endpoint's own, never a cloud host.
        assert backend.public_url(stored.key).startswith(base_url)
        presigned = backend.presigned_upload(
            asset_id=7,
            kind=MediaKind.AVATAR,
            size=len(AVATAR_BYTES),
            content_type=CONTENT_TYPE,
        )
        assert presigned.url.startswith(base_url)
        assert presigned.key == "avatars/7.png"
        assert presigned.method == "PUT"
        health = backend.health()
        assert health.reachable is True
        assert health.bucket == settings.minio_bucket
        assert health.endpoint == base_url
    finally:
        backend.delete(stored.key)

    assert backend.exists(stored.key) is False
