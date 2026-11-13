"""GET /api/media/health/storage: the backend, its bucket and whether it answers.

The backend is replaced through FastAPI's dependency override, so the route is exercised
against a bucket-backed backend, the local backend and one whose storage does not answer
-- without contacting a bucket. The local case is a real `LocalStorageBackend` over this
test's own temp directory, so `bucket` really is null, and the unreachable case proves the
refusal is the shared ProblemDetail envelope carrying `reachable: false`, never a 500.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from app.main import app
from app.media import (
    LocalStorageBackend,
    MediaSettings,
    StorageHealth,
    get_storage_backend,
)

health_storage_url = "/api/media/health/storage"
BUCKET = "bms-media"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
HEALTH_KEYS = {"backend", "bucket", "reachable"}
UNREACHABLE_KEYS = PROBLEM_DETAIL_KEYS | {"reachable"}


class _FakeStorage:
    """Stand-in for a backend: it reports exactly the health it was handed."""

    def __init__(self, health: StorageHealth) -> None:
        self.report = health
        self.calls = 0

    def health(self) -> StorageHealth:
        self.calls += 1
        return self.report


@contextmanager
def _use_storage(storage: object) -> Iterator[object]:
    """Serve the route this backend; the override is always popped again."""
    app.dependency_overrides[get_storage_backend] = lambda: storage
    try:
        yield storage
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def _local_backend(upload_dir: Path) -> LocalStorageBackend:
    """A real local backend over a directory of its own; app.state is never touched."""
    return LocalStorageBackend(
        replace(MediaSettings.from_env(), upload_dir=str(upload_dir))
    )


def test_health_storage_reports_the_bucket_of_the_s3_backend(client) -> None:
    fake = _FakeStorage(StorageHealth(backend="s3", bucket=BUCKET, reachable=True))

    with _use_storage(fake):
        response = client.get(health_storage_url)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == HEALTH_KEYS
    assert body == {"backend": "s3", "bucket": BUCKET, "reachable": True}
    assert fake.calls == 1


def test_health_storage_reports_the_local_backend_without_a_bucket(
    client, tmp_path: Path
) -> None:
    upload_dir = tmp_path / "media-upload"
    upload_dir.mkdir(parents=True, exist_ok=True)

    with _use_storage(_local_backend(upload_dir)):
        response = client.get(health_storage_url)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == HEALTH_KEYS
    assert body == {"backend": "local", "bucket": None, "reachable": True}


def test_an_unreachable_bucket_answers_503_with_the_problem_detail_body(client) -> None:
    fake = _FakeStorage(StorageHealth(backend="s3", bucket=BUCKET, reachable=False))

    with _use_storage(fake):
        response = client.get(health_storage_url)

    assert response.status_code == 503
    body = response.json()
    assert set(body) == UNREACHABLE_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"]
    assert BUCKET in body["detail"]
    assert body["instance"] == health_storage_url
    assert body["reachable"] is False


def test_an_unreachable_local_directory_answers_503_not_a_500(
    client, tmp_path: Path
) -> None:
    with _use_storage(_local_backend(tmp_path / "never-created")):
        response = client.get(health_storage_url)

    assert response.status_code == 503
    body = response.json()
    assert set(body) == UNREACHABLE_KEYS
    assert body["status"] == 503
    assert body["instance"] == health_storage_url
    assert body["reachable"] is False


def test_health_storage_needs_no_token(client) -> None:
    fake = _FakeStorage(StorageHealth(backend="s3", bucket=BUCKET, reachable=True))

    with _use_storage(fake):
        anonymous = client.get(health_storage_url, headers={})
        bogus = client.get(
            health_storage_url, headers={"Authorization": "Bearer not-a-token"}
        )

    assert anonymous.status_code == 200
    assert anonymous.json() == {
        "backend": "s3",
        "bucket": BUCKET,
        "reachable": True,
    }
    # An unauthenticated read ignores whatever token is sent rather than rejecting it.
    assert bogus.status_code == 200
