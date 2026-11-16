"""The public storage health report: which backend holds the bytes, and does it answer.

`GET /api/media/health/storage` carries no bearer token and no encryption session, so an
operator's first probe of a deployment works before any client handshake could exist, and
it is always a 200: a bucket that cannot be reached is a state to report rather than a
failed request. The bucket is a hand-written fake -- the same shape tests/test_s3_storage.py
uses -- so nothing here needs boto3, a network or a live bucket. The GCS cases use the
same trick: a fake that answers `bucket(name)` with itself, so nothing needs
google-cloud-storage credentials, a real bucket or a network either. The MinIO cases
reuse that bucket fake, because the MinIO backend speaks the same API, and assert the
endpoint and bucket the report now carries for it -- which stay empty for local, s3 and
gcs, exactly as they did before those two fields existed.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient

from app.crypto.config import CryptoSettings
from app.gcs_storage import GcsStorageBackend
from app.main import app, create_app
from app.media import MediaSettings, get_storage_backend
from app.minio_storage import MinioStorageBackend
from app.s3_storage import S3StorageBackend

HEALTH_URL = "/api/media/health/storage"
HEALTH_KEYS = {"backend", "reachable", "bucket", "endpoint"}
S3_BUCKET = "bms-media"
GCS_BUCKET = "bms-gcs-media"
MINIO_BUCKET = "bms-minio-media"
MINIO_ENDPOINT = "localhost:9000"
# The endpoint the report carries: a bare `host:port` with MINIO_SECURE off gains http.
MINIO_ENDPOINT_URL = f"http://{MINIO_ENDPOINT}"


class _BucketError(Exception):
    """A stand-in for botocore's ClientError, carrying the same `response` shape."""

    def __init__(self, code: str = "403", status_code: int = 403) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        }


class _FakeBucket:
    """The one call `health()` makes: an idempotent head of the bucket."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.head_bucket_calls: list[str] = []

    def head_bucket(self, *, Bucket: str) -> dict[str, Any]:
        self.head_bucket_calls.append(Bucket)
        if self.error is not None:
            raise self.error
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def _s3_settings(current: MediaSettings) -> MediaSettings:
    """This app's own settings, switched to a configured bucket."""
    return replace(current, storage_backend="s3", s3_bucket=S3_BUCKET)


class _FakeGcsBucket:
    """The one call the GCS backend's `health()` makes: an idempotent bucket probe.

    It answers `bucket(name)` with itself, so it is both the injected client and the
    handle the probe is issued on, and it records every bucket name it was asked for.
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.bucket_calls: list[str] = []
        self.exists_calls = 0

    def bucket(self, name: str) -> "_FakeGcsBucket":
        self.bucket_calls.append(name)
        return self

    def exists(self) -> bool:
        self.exists_calls += 1
        if self.error is not None:
            raise self.error
        return True


def _gcs_settings(current: MediaSettings) -> MediaSettings:
    """This app's own settings, switched to a configured GCS bucket."""
    return replace(current, storage_backend="gcs", gcs_bucket=GCS_BUCKET)


def _minio_settings(current: MediaSettings) -> MediaSettings:
    """This app's own settings, switched to a MinIO bucket on an http endpoint."""
    return replace(
        current,
        storage_backend="minio",
        minio_bucket=MINIO_BUCKET,
        minio_endpoint=MINIO_ENDPOINT,
        minio_secure=False,
    )


@contextmanager
def _use_storage(storage: object) -> Iterator[object]:
    """Serve this backend to the health route; the override is always popped again."""
    app.dependency_overrides[get_storage_backend] = lambda: storage
    try:
        yield storage
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def _assert_report(
    response, backend: str, reachable: bool, bucket: str = "", endpoint: str = ""
) -> None:
    """The body is exactly the four documented fields, whatever the backend state is."""
    assert response.status_code == 200
    body = response.json()
    assert set(body) == HEALTH_KEYS
    assert body == {
        "backend": backend,
        "reachable": reachable,
        "bucket": bucket,
        "endpoint": endpoint,
    }


def test_local_backend_reports_reachable(client) -> None:
    response = client.get(HEALTH_URL)

    _assert_report(response, "local", True)


def test_s3_backend_reports_reachable_and_unreachable(client) -> None:
    settings = _s3_settings(app.state.media_settings)
    bucket = _FakeBucket()
    healthy = S3StorageBackend(settings, client=bucket)

    with _use_storage(healthy):
        reachable = client.get(HEALTH_URL)

    _assert_report(reachable, "s3", True)
    # The probe is a real round trip to the configured bucket, not a guess.
    assert bucket.head_bucket_calls == [S3_BUCKET]

    denied = S3StorageBackend(settings, client=_FakeBucket(_BucketError()))

    with _use_storage(denied):
        unreachable = client.get(HEALTH_URL)

    # An unreachable bucket is reported, never raised: the status stays 200.
    _assert_report(unreachable, "s3", False)


def test_gcs_backend_reports_reachable_and_unreachable(client) -> None:
    settings = _gcs_settings(app.state.media_settings)
    bucket = _FakeGcsBucket()
    healthy = GcsStorageBackend(settings, client=bucket)

    with _use_storage(healthy):
        reachable = client.get(HEALTH_URL)

    _assert_report(reachable, "gcs", True)
    # The probe is a real round trip to the configured bucket, not a guess.
    assert bucket.bucket_calls == [GCS_BUCKET]
    assert bucket.exists_calls == 1

    denied = GcsStorageBackend(
        settings, client=_FakeGcsBucket(RuntimeError("bucket denied"))
    )

    with _use_storage(denied):
        unreachable = client.get(HEALTH_URL)

    # An unreachable bucket is reported, never raised: the status stays 200.
    _assert_report(unreachable, "gcs", False)

    # The override is gone, so the app's own backend answers again.
    _assert_report(client.get(HEALTH_URL), "local", True)


def test_health_needs_no_token(client) -> None:
    """No Authorization header, no session: the route is public on its own."""
    response = client.get(HEALTH_URL, headers={})

    assert "authorization" not in {
        name.lower() for name in response.request.headers
    }
    _assert_report(response, "local", True)


def test_health_is_reachable_with_crypto_enabled() -> None:
    """The route sits on the encryption layer's public list, so it reads without one."""
    application = create_app(CryptoSettings(enabled=True, private_key_pem=None))

    with TestClient(application) as encrypted_client:
        local = encrypted_client.get(HEALTH_URL)

        settings = _s3_settings(application.state.media_settings)
        broken = S3StorageBackend(
            settings, client=_FakeBucket(RuntimeError("endpoint down"))
        )
        application.dependency_overrides[get_storage_backend] = lambda: broken
        try:
            unreachable = encrypted_client.get(HEALTH_URL)
        finally:
            application.dependency_overrides.pop(get_storage_backend, None)

    # A plain body, not an envelope: the middleware never saw these requests.
    assert local.headers["content-type"] == "application/json"
    assert unreachable.headers["content-type"] == "application/json"
    _assert_report(local, "local", True)
    _assert_report(unreachable, "s3", False)


def test_gcs_health_is_reachable_with_crypto_enabled() -> None:
    """The same public route reports a GCS bucket with the encryption layer switched on."""
    application = create_app(CryptoSettings(enabled=True, private_key_pem=None))

    with TestClient(application) as encrypted_client:
        settings = _gcs_settings(application.state.media_settings)

        healthy = GcsStorageBackend(settings, client=_FakeGcsBucket())
        application.dependency_overrides[get_storage_backend] = lambda: healthy
        try:
            reachable = encrypted_client.get(HEALTH_URL)
        finally:
            application.dependency_overrides.pop(get_storage_backend, None)

        broken = GcsStorageBackend(
            settings, client=_FakeGcsBucket(RuntimeError("bucket denied"))
        )
        application.dependency_overrides[get_storage_backend] = lambda: broken
        try:
            unreachable = encrypted_client.get(HEALTH_URL)
        finally:
            application.dependency_overrides.pop(get_storage_backend, None)

    # A plain body, not an envelope: the route is on the middleware's public list.
    assert reachable.headers["content-type"] == "application/json"
    assert unreachable.headers["content-type"] == "application/json"
    _assert_report(reachable, "gcs", True)
    _assert_report(unreachable, "gcs", False)


def test_minio_backend_reports_endpoint_and_bucket(client) -> None:
    """A MinIO backend names the bucket and the endpoint it is pointed at."""
    settings = _minio_settings(app.state.media_settings)
    bucket = _FakeBucket()
    healthy = MinioStorageBackend(settings, client=bucket)

    with _use_storage(healthy):
        reachable = client.get(HEALTH_URL)

    _assert_report(reachable, "minio", True, MINIO_BUCKET, MINIO_ENDPOINT_URL)
    # The probe is a real round trip to the configured bucket, not a guess.
    assert bucket.head_bucket_calls == [MINIO_BUCKET]

    broken = MinioStorageBackend(
        settings, client=_FakeBucket(RuntimeError("endpoint down"))
    )

    with _use_storage(broken):
        unreachable = client.get(HEALTH_URL)

    # An unreachable endpoint is reported, never raised: the status stays 200.
    _assert_report(unreachable, "minio", False, MINIO_BUCKET, MINIO_ENDPOINT_URL)

    # The override is gone, so the app's own backend answers again.
    _assert_report(client.get(HEALTH_URL), "local", True)


def test_minio_health_is_reachable_with_crypto_enabled() -> None:
    """The extended body stays plain JSON with the encryption layer switched on."""
    application = create_app(CryptoSettings(enabled=True, private_key_pem=None))

    with TestClient(application) as encrypted_client:
        settings = _minio_settings(application.state.media_settings)

        healthy = MinioStorageBackend(settings, client=_FakeBucket())
        application.dependency_overrides[get_storage_backend] = lambda: healthy
        try:
            reachable = encrypted_client.get(HEALTH_URL)
        finally:
            application.dependency_overrides.pop(get_storage_backend, None)

        broken = MinioStorageBackend(
            settings, client=_FakeBucket(RuntimeError("endpoint down"))
        )
        application.dependency_overrides[get_storage_backend] = lambda: broken
        try:
            unreachable = encrypted_client.get(HEALTH_URL)
        finally:
            application.dependency_overrides.pop(get_storage_backend, None)

    # A plain body, not an envelope: the route is on the middleware's public list.
    assert reachable.headers["content-type"] == "application/json"
    assert unreachable.headers["content-type"] == "application/json"
    _assert_report(reachable, "minio", True, MINIO_BUCKET, MINIO_ENDPOINT_URL)
    _assert_report(unreachable, "minio", False, MINIO_BUCKET, MINIO_ENDPOINT_URL)
