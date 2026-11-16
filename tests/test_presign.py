"""The signed upload token and the exact-size save a direct upload is stored through.

The token is the whole authorization a direct-to-storage upload carries, so its
round trip, its expiry, a foreign signature and a rewrite of its claims are checked
here without going through the router -- and `save_exact` is checked to store a body
only when it is exactly the declared number of bytes, leaving neither a file nor a
`.part` file behind when it is not.
"""

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from pathlib import Path

import jwt
import pytest

from app.media import (
    ExactSizeMismatchError,
    LocalStorageBackend,
    MediaSettings,
    RequestStreamChunks,
    StoredObject,
    object_key,
)
from app.presign import (
    PresignTokenError,
    decode_upload_token,
    sign_upload_token,
    upload_url,
)
from app.schemas import MediaKind

AVATAR_BYTES = b"\x89PNG\r\n\x1a\navatar-bytes"
CONTENT_TYPE = "image/png"


def _settings(tmp_path: Path, **overrides: object) -> MediaSettings:
    """The app's own settings, pointed at this test's temp directory."""
    values: dict[str, object] = {
        "storage_backend": "local",
        "upload_dir": str(tmp_path / "media"),
        "max_image_bytes": 1024,
        "max_video_bytes": 2048,
        "service_token": "test-service-token",
        "presign_secret": "test-presign-secret",
        "presign_expires_seconds": 900,
    }
    values.update(overrides)
    return MediaSettings(**values)  # type: ignore[arg-type]


async def _body(payload: bytes, chunk_size: int = 4) -> AsyncIterator[bytes]:
    """The chunks a server would hand storage for this body."""
    for start in range(0, len(payload), chunk_size):
        yield payload[start : start + chunk_size]


async def _save_exact(
    backend: LocalStorageBackend, payload: bytes, *, expected_size: int
) -> StoredObject:
    return await backend.save_exact(
        kind=MediaKind.AVATAR,
        content_type=CONTENT_TYPE,
        upload=RequestStreamChunks(_body(payload)),
        expected_size=expected_size,
    )


async def _drain(chunks: RequestStreamChunks) -> bytes:
    """Read the source to its end, guarding against a body that never ends."""
    pieces: list[bytes] = []
    while True:
        piece = await chunks.read(3)
        if not piece:
            return b"".join(pieces)
        pieces.append(piece)
        if len(pieces) > 20:
            raise AssertionError("the body never ended")


def _stored_files(settings: MediaSettings) -> set[Path]:
    """Every file under the upload directory, .part leftovers included."""
    root = Path(settings.upload_dir)
    return {path for path in root.rglob("*") if path.is_file()}


def test_signed_token_round_trips_its_grant(tmp_path) -> None:
    settings = _settings(tmp_path)

    token = sign_upload_token(
        settings,
        asset_id=7,
        kind=MediaKind.AVATAR,
        size=len(AVATAR_BYTES),
        content_type=CONTENT_TYPE,
    )
    grant = decode_upload_token(settings, token=token)

    assert grant.asset_id == 7
    assert grant.kind is MediaKind.AVATAR
    assert grant.size == len(AVATAR_BYTES)
    assert grant.content_type == CONTENT_TYPE


def test_default_lifetime_is_the_configured_one(tmp_path) -> None:
    settings = _settings(tmp_path, presign_expires_seconds=900)

    token = sign_upload_token(
        settings,
        asset_id=5,
        kind=MediaKind.AVATAR,
        size=4,
        content_type=CONTENT_TYPE,
    )
    claims = jwt.decode(
        token,
        settings.presign_secret,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )

    assert claims["assetId"] == 5
    assert claims["kind"] == "AVATAR"
    assert claims["size"] == 4
    assert claims["contentType"] == CONTENT_TYPE
    assert 890 <= claims["exp"] - time.time() <= 900


def test_expired_token_is_refused(tmp_path) -> None:
    settings = _settings(tmp_path)

    token = sign_upload_token(
        settings,
        asset_id=7,
        kind=MediaKind.AVATAR,
        size=4,
        content_type=CONTENT_TYPE,
        expires_in_seconds=-60,
    )

    with pytest.raises(PresignTokenError):
        decode_upload_token(settings, token=token)


def test_token_signed_with_another_secret_is_refused(tmp_path) -> None:
    sender = _settings(tmp_path, presign_secret="another-secret-entirely")
    receiver = _settings(tmp_path, presign_secret="the-configured-secret")

    token = sign_upload_token(
        sender,
        asset_id=7,
        kind=MediaKind.AVATAR,
        size=4,
        content_type=CONTENT_TYPE,
    )

    with pytest.raises(PresignTokenError):
        decode_upload_token(receiver, token=token)


def test_altered_claims_are_refused(tmp_path) -> None:
    settings = _settings(tmp_path)
    token = sign_upload_token(
        settings,
        asset_id=7,
        kind=MediaKind.AVATAR,
        size=4,
        content_type=CONTENT_TYPE,
    )
    header, claims, signature = token.split(".")
    rewritten_claim = ("a" if claims[0] != "a" else "b") + claims[1:]
    rewritten = f"{header}.{rewritten_claim}.{signature}"

    with pytest.raises(PresignTokenError):
        decode_upload_token(settings, token=rewritten)

    with pytest.raises(PresignTokenError):
        decode_upload_token(settings, token="not-a-token")


def test_upload_url_names_the_asset_and_carries_its_token(tmp_path) -> None:
    settings = _settings(tmp_path)

    url = upload_url(
        settings,
        asset_id=7,
        kind=MediaKind.AVATAR,
        size=len(AVATAR_BYTES),
        content_type=CONTENT_TYPE,
    )

    prefix = "/api/media/7/upload?token="
    assert url.startswith(prefix)
    grant = decode_upload_token(settings, token=url[len(prefix) :])
    # A token minted for asset 7 is only good for asset 7: the grant says which.
    assert grant.asset_id == 7

    other = upload_url(
        settings,
        asset_id=8,
        kind=MediaKind.AVATAR,
        size=len(AVATAR_BYTES),
        content_type=CONTENT_TYPE,
    )
    assert decode_upload_token(
        settings, token=other[len("/api/media/8/upload?token=") :]
    ).asset_id == 8


def test_save_exact_stores_a_body_of_the_declared_size(tmp_path) -> None:
    settings = _settings(tmp_path)
    backend = LocalStorageBackend(settings)

    stored = asyncio.run(
        _save_exact(backend, AVATAR_BYTES, expected_size=len(AVATAR_BYTES))
    )

    assert stored.size == len(AVATAR_BYTES)
    assert stored.sha256 == hashlib.sha256(AVATAR_BYTES).hexdigest()
    assert stored.key == object_key(
        kind=MediaKind.AVATAR, content_type=CONTENT_TYPE, sha256=stored.sha256
    )
    assert stored.key.startswith("avatars/")
    assert backend.path_for(stored.key).read_bytes() == AVATAR_BYTES


@pytest.mark.parametrize("delta", [-1, 1])
def test_save_exact_refuses_a_body_off_the_declared_size(
    tmp_path, delta: int
) -> None:
    settings = _settings(tmp_path)
    backend = LocalStorageBackend(settings)
    expected = len(AVATAR_BYTES) + delta

    with pytest.raises(ExactSizeMismatchError) as caught:
        asyncio.run(_save_exact(backend, AVATAR_BYTES, expected_size=expected))

    assert caught.value.expected == expected
    assert caught.value.actual == len(AVATAR_BYTES)
    # Neither a stored file nor a .part leftover is left behind.
    assert _stored_files(settings) == set()


def test_request_stream_chunks_yields_slices_then_nothing() -> None:
    chunks = RequestStreamChunks(_body(AVATAR_BYTES))

    assert asyncio.run(_drain(chunks)) == AVATAR_BYTES
    assert asyncio.run(chunks.read(3)) == b""
    assert asyncio.run(RequestStreamChunks(_body(b"")).read(3)) == b""
