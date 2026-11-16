"""The public storage health report: which backend holds the bytes, and does it answer.

`GET /api/media/health/storage` carries no bearer token and no encryption session, so an
operator's first probe of a deployment works before any client handshake could exist, and
it is always a 200: a bucket that cannot be reached is a state to report rather than a
failed request. The bucket is a hand-written fake -- the same shape tests/test_s3_storage.py
uses -- so nothing here needs boto3, a network or a live bucket.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient

from app.crypto.config import CryptoSettings
from app.main import app, create_app
from app.media import MediaSettings, get_storage_backend
from app.s3_storage import S3StorageBackend

HEALTH_URL = "/api/media/health/storage"
HEALTH_KEYS = {"backend", "reachable"}
S3_BUCKET = "bms-media"


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


@contextmanager
def _use_storage(storage: object) -> Iterator[object]:
    """Serve this backend to the health route; the override is always popped again."""
    app.dependency_overrides[get_storage_backend] = lambda: storage
    try:
        yield storage
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def _assert_report(response, backend: str, reachable: bool) -> None:
    """The body is exactly the two documented fields, whatever the backend state is."""
    assert response.status_code == 200
    body = response.json()
    assert set(body) == HEALTH_KEYS
    assert body == {"backend": backend, "reachable": reachable}


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
