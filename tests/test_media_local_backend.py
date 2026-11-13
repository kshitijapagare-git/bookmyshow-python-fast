"""The local backend's read/delete/presign/health half of the widened storage seam.

`LocalStorageBackend` is still the default backend, and tasks/complete/streaming now go
through the same object operations an S3 backend answers, so they are exercised here
directly rather than through a route: the backend is pointed at a fresh temp directory,
bytes are stored with `save_stream`, and `exists`/`digest`/`iter_chunks`/`delete` are
checked against exactly those bytes. The async operations are driven with `asyncio.run`
because the suite has no async test plugin, and `presigned_upload_url` is checked with
the same `verify_upload_token` the local `/upload` route verifies with, so the local
presign cannot drift from the HMAC route that consumes it.
"""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from app.media import (
    CHUNK_SIZE,
    DEFAULT_STORAGE_BACKEND,
    DigestResult,
    LocalStorageBackend,
    MediaSettings,
    StoredObject,
    integrity_report,
)
from app.media_presign import verify_upload_token
from app.schemas import MediaKind

PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\navatar-bytes"
BIG_PAYLOAD = b"x" * (CHUNK_SIZE + 512)
CONTENT_TYPE = "image/png"
ASSET_ID = 7
EXPIRES_SECONDS = 300


@pytest.fixture()
def backend(tmp_path: Path) -> LocalStorageBackend:
    """A local backend over a directory of its own; app.state is never touched."""
    settings = replace(
        MediaSettings.from_env(), upload_dir=str(tmp_path / "media-upload")
    )
    return LocalStorageBackend(settings)


async def _chunks(payload: bytes) -> AsyncIterator[bytes]:
    """One chunk, so `save_stream` sees exactly this payload."""
    yield payload


def _store(
    backend: LocalStorageBackend, payload: bytes = PHOTO_PAYLOAD
) -> StoredObject:
    return asyncio.run(
        backend.save_stream(
            kind=MediaKind.AVATAR,
            content_type=CONTENT_TYPE,
            chunks=_chunks(payload),
            max_bytes=len(payload),
        )
    )


async def _collect(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in chunks])


def test_exists_is_false_until_the_bytes_land_and_after_they_are_deleted(
    backend: LocalStorageBackend,
) -> None:
    assert asyncio.run(backend.exists(key="avatars/never-written.png")) is False

    stored = _store(backend)

    assert asyncio.run(backend.exists(key=stored.key)) is True

    asyncio.run(backend.delete(key=stored.key))

    assert asyncio.run(backend.exists(key=stored.key)) is False
    # Deleting a key that is already gone is not an error.
    asyncio.run(backend.delete(key=stored.key))


def test_digest_answers_the_saved_digest_and_none_for_a_missing_key(
    backend: LocalStorageBackend,
) -> None:
    stored = _store(backend)

    recomputed = asyncio.run(backend.digest(key=stored.key, algorithm="sha256"))

    assert isinstance(recomputed, DigestResult)
    assert recomputed.sha256 == stored.sha256
    assert recomputed.sha256 == hashlib.sha256(PHOTO_PAYLOAD).hexdigest()
    assert recomputed.size == stored.size == len(PHOTO_PAYLOAD)

    assert (
        asyncio.run(backend.digest(key="avatars/missing.png", algorithm="sha256"))
        is None
    )


def test_iter_chunks_streams_the_object_and_honours_offset_and_length(
    backend: LocalStorageBackend,
) -> None:
    stored = _store(backend, BIG_PAYLOAD)

    whole = asyncio.run(_collect(backend.iter_chunks(key=stored.key)))
    assert whole == BIG_PAYLOAD

    sliced = asyncio.run(
        _collect(
            backend.iter_chunks(key=stored.key, offset=CHUNK_SIZE - 10, length=20)
        )
    )
    assert sliced == BIG_PAYLOAD[CHUNK_SIZE - 10 : CHUNK_SIZE - 10 + 20]

    tail = asyncio.run(_collect(backend.iter_chunks(key=stored.key, offset=CHUNK_SIZE)))

    assert tail == BIG_PAYLOAD[CHUNK_SIZE:]


def test_presigned_upload_url_matches_the_local_hmac_route(
    backend: LocalStorageBackend,
) -> None:
    presigned = backend.presigned_upload_url(
        asset_id=ASSET_ID,
        key="avatars/reserved.png",
        content_type=CONTENT_TYPE,
        expires_seconds=EXPIRES_SECONDS,
    )

    assert presigned.method == "PUT"
    assert presigned.headers == {"Content-Type": CONTENT_TYPE}
    assert presigned.url.startswith(f"/api/media/{ASSET_ID}/upload?token=")

    token = presigned.url.partition("token=")[2]
    expires_at = verify_upload_token(
        token=token, asset_id=ASSET_ID, secret=backend.settings.presign_secret
    )

    assert expires_at > 0


def test_presigned_download_url_is_the_public_url(
    backend: LocalStorageBackend,
) -> None:
    stored = _store(backend)

    assert (
        backend.presigned_download_url(key=stored.key, expires_seconds=60)
        == backend.public_url(stored.key)
        == f"/media/{stored.key}"
    )


def test_prepare_key_is_empty_because_a_local_key_is_content_addressed(
    backend: LocalStorageBackend,
) -> None:
    assert backend.prepare_key(kind=MediaKind.AVATAR, content_type=CONTENT_TYPE) == ""


def test_health_follows_the_upload_directory(backend: LocalStorageBackend) -> None:
    upload_dir = Path(backend.settings.upload_dir)

    missing = backend.health()

    assert missing.backend == DEFAULT_STORAGE_BACKEND == "local"
    assert missing.bucket is None
    assert missing.reachable is False

    upload_dir.mkdir(parents=True, exist_ok=True)

    present = backend.health()

    assert present.backend == "local"
    assert present.bucket is None
    assert present.reachable is True


def test_integrity_report_is_a_coroutine_that_reads_through_the_backend(
    backend: LocalStorageBackend,
) -> None:
    stored = _store(backend)
    expected = (stored.sha256, stored.size)

    untouched = asyncio.run(
        integrity_report(
            storage=backend,
            storage_key=stored.key,
            expected_sha256=stored.sha256,
            expected_size=stored.size,
            algorithm="sha256",
        )
    )

    assert untouched.verified is True
    assert (untouched.sha256, untouched.size) == expected
    assert untouched.recomputed_sha256 == stored.sha256
    assert untouched.recomputed_size == stored.size

    backend.path_for(stored.key).write_bytes(b"tampered-after-upload")

    tampered = asyncio.run(
        integrity_report(
            storage=backend,
            storage_key=stored.key,
            expected_sha256=stored.sha256,
            expected_size=stored.size,
            algorithm="sha256",
        )
    )

    # The recorded values are still what the row says; only the trust flag moves.
    assert tampered.verified is False
    assert (tampered.sha256, tampered.size) == expected
    assert tampered.recomputed_sha256 == hashlib.sha256(
        b"tampered-after-upload"
    ).hexdigest()

    asyncio.run(backend.delete(key=stored.key))

    gone = asyncio.run(
        integrity_report(
            storage=backend,
            storage_key=stored.key,
            expected_sha256=stored.sha256,
            expected_size=stored.size,
            algorithm="sha256",
        )
    )

    assert gone.verified is False
    assert gone.recomputed_sha256 is None
    assert gone.recomputed_size is None
