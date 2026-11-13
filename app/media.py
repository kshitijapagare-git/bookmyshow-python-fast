"""Storage for uploaded media: movie posters, backdrops, trailers, review photos and avatars.

The bytes go through one `StorageBackend`, so the router never touches a filesystem
or an SDK directly: `LocalStorageBackend` streams the upload in `CHUNK_SIZE` chunks
into a `.part` file inside the destination directory, hashing the bytes with SHA-256
as they go, and only then `os.replace`s the temp file into its final, content-addressed
key. Saving the same bytes twice therefore lands on the same key, and an oversized or
empty upload leaves nothing behind at all. A multipart `save` is only an adapter over
`save_stream`, so the bytes of a direct presigned upload and of a multipart one are
written by the same code and end up under the same kind of key.

`MediaSettings.from_env()` reads the knobs -- the hash algorithm, the two presign
ones and the seven S3 ones included -- with the same fall-back-not-raise convention
as `PaymentsSettings` and `ReviewsSettings`: an absent, blank or unparseable value
selects the default instead of raising.

Beyond writing bytes, the seam also reads them back -- `digest`, `iter_chunks`, `exists`
and `delete` -- and hands out `presigned_upload_url`/`presigned_download_url` and a
`health` report, so a router never learns whether the object lives on a local disk or in
a bucket. `path_for` is not part of the protocol: only a local backend has a disk path.

`integrity_report` is the read-back seam for the integrity and verify routes: it streams
the stored object through the backend's `digest` and compares the result in constant time
with the digest recorded at upload, so a replaced object reads as untrusted.
"""

import hashlib
import logging
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fastapi import Request, UploadFile

from .media_presign import sign_upload_token
from .schemas import MediaKind

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_BACKEND = "local"
S3_STORAGE_BACKEND = "s3"
DEFAULT_UPLOAD_DIR = "media"
DEFAULT_S3_MEDIA_PREFIX = "media"
S3_SSE_AES256 = "AES256"
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_VIDEO_BYTES = 100 * 1024 * 1024
DEFAULT_MEDIA_SERVICE_TOKEN = "dev-service-token"
DEFAULT_PRESIGN_SECRET = "dev-presign-secret"
DEFAULT_PRESIGN_EXPIRES_SECONDS = 300
DEFAULT_HASH_ALGORITHM = "sha256"
SUPPORTED_HASH_ALGORITHMS: frozenset[str] = frozenset({DEFAULT_HASH_ALGORITHM})

CHUNK_SIZE = 1024 * 1024
MEDIA_URL_PREFIX = "/media"
PART_SUFFIX = ".part"

KIND_SUBDIRS: dict[MediaKind, str] = {
    MediaKind.POSTER: "posters",
    MediaKind.BACKDROP: "backdrops",
    MediaKind.TRAILER: "trailers",
    MediaKind.REVIEW_PHOTO: "review-photos",
    MediaKind.AVATAR: "avatars",
}

ALLOWED_CONTENT_TYPES: dict[MediaKind, dict[str, str]] = {
    MediaKind.POSTER: {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    },
    MediaKind.BACKDROP: {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    },
    MediaKind.TRAILER: {
        "video/mp4": ".mp4",
    },
    MediaKind.REVIEW_PHOTO: {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    },
    MediaKind.AVATAR: {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    },
}

IMAGE_KINDS: tuple[MediaKind, ...] = (
    MediaKind.POSTER,
    MediaKind.BACKDROP,
    MediaKind.REVIEW_PHOTO,
    MediaKind.AVATAR,
)


def _env(name: str, default: str) -> str:
    """The environment value, or the default when it is absent or blank."""
    return (os.getenv(name) or "").strip() or default


def _positive_int(name: str, default: int) -> int:
    """A positive integer from the environment; anything else falls back."""
    raw = _env(name, "")
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def new_hasher(algorithm: str) -> Any:
    """A hashlib digest for `algorithm`; an unsupported name falls back to sha256.

    Only sha256 is supported, and the recorded column is `sha256`, so anything else
    warns and hashes with sha256 rather than writing a different digest under that name.
    """
    if algorithm not in SUPPORTED_HASH_ALGORITHMS:
        logger.warning(
            "unsupported MEDIA_HASH_ALGORITHM %r, using %r",
            algorithm,
            DEFAULT_HASH_ALGORITHM,
        )
    return hashlib.sha256()


def digest_of_file(
    *, path: Path, algorithm: str, chunk_size: int = CHUNK_SIZE
) -> tuple[str, int]:
    """Stream `path` through `algorithm`, answering its hex digest and byte count."""
    digest = new_hasher(algorithm)
    size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class MediaSettings:
    """Where uploads are stored, how large each kind may be and who may post them."""

    storage_backend: str
    upload_dir: str
    max_image_bytes: int
    max_video_bytes: int
    service_token: str
    hash_algorithm: str = DEFAULT_HASH_ALGORITHM
    presign_secret: str = DEFAULT_PRESIGN_SECRET
    presign_expires_seconds: int = DEFAULT_PRESIGN_EXPIRES_SECONDS
    # Where the S3 backend puts the objects; every S3_* value is optional, so a host
    # that configures none of them simply keeps the local backend.
    s3_bucket: str = ""
    s3_region: str = ""
    # An empty endpoint means real AWS; a set one points at an S3-compatible server.
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    # Never logged and never serialised into a response.
    s3_secret_access_key: str = ""
    s3_media_prefix: str = DEFAULT_S3_MEDIA_PREFIX
    # Empty means no server-side encryption; only S3_SSE=AES256 turns it on.
    s3_sse: str = ""

    @classmethod
    def from_env(cls) -> "MediaSettings":
        """The configured settings; anything unrecognised falls back, never raises."""
        configured = _env("MEDIA_HASH_ALGORITHM", "").lower()
        configured_sse = _env("S3_SSE", "")
        return cls(
            storage_backend=_env(
                "MEDIA_STORAGE_BACKEND", DEFAULT_STORAGE_BACKEND
            ).lower(),
            upload_dir=_env("MEDIA_UPLOAD_DIR", DEFAULT_UPLOAD_DIR),
            max_image_bytes=_positive_int(
                "MEDIA_MAX_IMAGE_BYTES", DEFAULT_MAX_IMAGE_BYTES
            ),
            max_video_bytes=_positive_int(
                "MEDIA_MAX_VIDEO_BYTES", DEFAULT_MAX_VIDEO_BYTES
            ),
            service_token=_env("MEDIA_SERVICE_TOKEN", DEFAULT_MEDIA_SERVICE_TOKEN),
            hash_algorithm=(
                configured
                if configured in SUPPORTED_HASH_ALGORITHMS
                else DEFAULT_HASH_ALGORITHM
            ),
            presign_secret=_env("MEDIA_PRESIGN_SECRET", DEFAULT_PRESIGN_SECRET),
            presign_expires_seconds=_positive_int(
                "MEDIA_PRESIGN_EXPIRES_SECONDS", DEFAULT_PRESIGN_EXPIRES_SECONDS
            ),
            s3_bucket=_env("S3_BUCKET", ""),
            s3_region=_env("S3_REGION", ""),
            s3_endpoint_url=_env("S3_ENDPOINT_URL", ""),
            s3_access_key_id=_env("S3_ACCESS_KEY_ID", ""),
            s3_secret_access_key=_env("S3_SECRET_ACCESS_KEY", ""),
            s3_media_prefix=_env("S3_MEDIA_PREFIX", DEFAULT_S3_MEDIA_PREFIX),
            s3_sse=S3_SSE_AES256 if configured_sse == S3_SSE_AES256 else "",
        )


def max_bytes_for(kind: MediaKind, settings: MediaSettings) -> int:
    """The configured byte limit: the image one for image kinds, else the video one."""
    if kind in IMAGE_KINDS:
        return settings.max_image_bytes
    return settings.max_video_bytes


class MediaTooLargeError(Exception):
    """The upload crossed the byte limit for its kind; the partial file is removed."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"upload exceeds the {limit} byte limit")
        self.limit = limit


class StorageUnreachableError(Exception):
    """The backend's storage does not answer; the health route answers it as a 503.

    It lives here rather than beside the S3 adapter so the error handler needs no
    boto3 import, and so a future remote backend reports the same way.
    """

    def __init__(self, *, backend: str, bucket: str | None, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.backend = backend
        self.bucket = bucket


class EmptyUploadError(Exception):
    """The upload carried no bytes at all."""


class UnsupportedMediaTypeError(Exception):
    """The declared content type is not on the allow-list for the requested kind."""

    def __init__(self, content_type: str) -> None:
        super().__init__(f"unsupported content type: {content_type}")
        self.content_type = content_type


@dataclass(frozen=True)
class StoredObject:
    """What one save ended as: the key it landed on, its size and its content hash."""

    key: str
    size: int
    sha256: str


@dataclass(frozen=True)
class IntegrityResult:
    """One integrity read: the values recorded at upload and the ones recomputed now.

    `sha256` and `size` are what the `MediaAsset` row says it stored; `recomputed_*`
    are what the bytes on disk hash to right now, and are None when the object is gone.
    """

    sha256: str
    size: int
    verified: bool
    recomputed_sha256: str | None
    recomputed_size: int | None


def object_key(*, kind: MediaKind, content_type: str, sha256: str) -> str:
    """`{subdir}/{sha256}{ext}`: the posting of identical bytes twice is one file."""
    return f"{KIND_SUBDIRS[kind]}/{sha256}{ALLOWED_CONTENT_TYPES[kind][content_type]}"


@dataclass(frozen=True)
class PresignedUpload:
    """Where the browser sends the bytes itself: the url, the method and the headers."""

    url: str
    method: str
    headers: dict[str, str]


@dataclass(frozen=True)
class StorageHealth:
    """One reachability read: which backend, which bucket and whether it answers.

    `bucket` is None for the local backend, which has a directory rather than a bucket.
    """

    backend: str
    bucket: str | None
    reachable: bool


@dataclass(frozen=True)
class DigestResult:
    """The digest and byte count of one stored object, as `digest` recomputed them."""

    sha256: str
    size: int


class StorageBackend(Protocol):
    """The one seam between the routes and wherever the bytes actually live."""

    async def save(
        self,
        *,
        kind: MediaKind,
        content_type: str,
        upload: UploadFile,
        max_bytes: int,
    ) -> StoredObject:
        """Stream `upload` into storage and answer the key, size and SHA-256 it got."""
        ...

    async def save_stream(
        self,
        *,
        kind: MediaKind,
        content_type: str,
        chunks: AsyncIterator[bytes],
        max_bytes: int,
    ) -> StoredObject:
        """Stream raw chunks into storage and answer the key, size and SHA-256 they got."""
        ...

    def prepare_key(self, *, kind: MediaKind, content_type: str) -> str:
        """The key reserved before the bytes arrive, or "" when it needs the digest."""
        ...

    async def exists(self, *, key: str) -> bool:
        """Whether an object is stored under `key` right now."""
        ...

    async def digest(self, *, key: str, algorithm: str) -> DigestResult | None:
        """The object's digest and size, or None when nothing is stored under `key`."""
        ...

    def iter_chunks(
        self, *, key: str, offset: int = 0, length: int | None = None
    ) -> AsyncIterator[bytes]:
        """Yield at most `CHUNK_SIZE` of the object at a time; `length` None means all."""
        ...

    async def delete(self, *, key: str) -> None:
        """Remove the object under `key`; a key that is already gone is not an error."""
        ...

    def presigned_upload_url(
        self,
        *,
        asset_id: int,
        key: str,
        content_type: str,
        expires_seconds: int,
    ) -> PresignedUpload:
        """The url, method and headers a client uploads this asset's bytes with."""
        ...

    def presigned_download_url(self, *, key: str, expires_seconds: int) -> str:
        """A short-lived url the stored object is readable from."""
        ...

    def public_url(self, key: str) -> str:
        """The public path the stored object is served from."""
        ...

    def health(self) -> StorageHealth:
        """Whether this backend's storage answers; must never raise."""
        ...


class LocalStorageBackend:
    """Streams uploads onto local disk, content-addressed by their SHA-256."""

    def __init__(self, settings: MediaSettings) -> None:
        self.settings = settings

    def path_for(self, key: str) -> Path:
        """`MEDIA_UPLOAD_DIR` joined with the key; the key is built here, never by callers."""
        return Path(self.settings.upload_dir) / key

    def public_url(self, key: str) -> str:
        """`/media/{key}`, served by the static mount `create_app` installs."""
        return f"{MEDIA_URL_PREFIX}/{key}"

    def prepare_key(self, *, kind: MediaKind, content_type: str) -> str:
        """The empty key: a local key is content-addressed, known only after the bytes hash."""
        return ""

    async def save(
        self,
        *,
        kind: MediaKind,
        content_type: str,
        upload: UploadFile,
        max_bytes: int,
    ) -> StoredObject:
        """Adapt a multipart upload onto `save_stream`, one `CHUNK_SIZE` read at a time."""
        return await self.save_stream(
            kind=kind,
            content_type=content_type,
            chunks=self._upload_chunks(upload),
            max_bytes=max_bytes,
        )

    async def _upload_chunks(self, upload: UploadFile) -> AsyncIterator[bytes]:
        """Yield the upload's bytes as they arrive, so it is never read whole."""
        while True:
            chunk = await upload.read(CHUNK_SIZE)
            if not chunk:
                return
            yield chunk

    async def save_stream(
        self,
        *,
        kind: MediaKind,
        content_type: str,
        chunks: AsyncIterator[bytes],
        max_bytes: int,
    ) -> StoredObject:
        """Copy the chunk stream to a temp file, then move it onto its sha-derived key."""
        destination_dir = Path(self.settings.upload_dir) / KIND_SUBDIRS[kind]
        destination_dir.mkdir(parents=True, exist_ok=True)
        part_path = destination_dir / f"{uuid.uuid4().hex}{PART_SUFFIX}"

        digest = new_hasher(self.settings.hash_algorithm)
        size = 0
        try:
            with part_path.open("wb") as sink:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaTooLargeError(max_bytes)
                    digest.update(chunk)
                    sink.write(chunk)
            if size == 0:
                raise EmptyUploadError()

            sha256 = digest.hexdigest()
            key = object_key(kind=kind, content_type=content_type, sha256=sha256)
            os.replace(part_path, self.path_for(key))
        except BaseException:
            # A refused upload must leave neither a partial file nor a .part file.
            part_path.unlink(missing_ok=True)
            raise

        return StoredObject(key=key, size=size, sha256=sha256)

    async def exists(self, *, key: str) -> bool:
        """Whether the key's file is on disk right now."""
        return self.path_for(key).is_file()

    async def digest(self, *, key: str, algorithm: str) -> DigestResult | None:
        """Stream the stored file through `algorithm`; None when the file is not there."""
        path = self.path_for(key)
        if not path.is_file():
            return None
        sha256, size = digest_of_file(path=path, algorithm=algorithm)
        return DigestResult(sha256=sha256, size=size)

    async def iter_chunks(
        self, *, key: str, offset: int = 0, length: int | None = None
    ) -> AsyncIterator[bytes]:
        """Read the stored file one `CHUNK_SIZE` at a time, seeking to `offset` first."""
        remaining = length
        if remaining is not None and remaining <= 0:
            return
        with self.path_for(key).open("rb") as source:
            if offset:
                source.seek(offset)
            while True:
                want = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
                if want <= 0:
                    return
                chunk = source.read(want)
                if not chunk:
                    return
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk

    async def delete(self, *, key: str) -> None:
        """Remove the stored file; a key that is already gone is not an error."""
        self.path_for(key).unlink(missing_ok=True)

    def presigned_upload_url(
        self,
        *,
        asset_id: int,
        key: str,
        content_type: str,
        expires_seconds: int,
    ) -> PresignedUpload:
        """The signed local `/upload` url: no network call, so the route stays sync.

        The key is deliberately unused: a local upload is addressed by its asset id,
        and the token is what authorizes it.
        """
        token = sign_upload_token(
            asset_id=asset_id,
            expires_at=int(time.time()) + expires_seconds,
            secret=self.settings.presign_secret,
        )
        return PresignedUpload(
            url=f"/api/media/{asset_id}/upload?token={token}",
            method="PUT",
            headers={"Content-Type": content_type},
        )

    def presigned_download_url(self, *, key: str, expires_seconds: int) -> str:
        """The bytes are public through the app's own `/media` route, so no token is needed."""
        return self.public_url(key)

    def health(self) -> StorageHealth:
        """Reachable exactly when the upload directory exists; never raises."""
        try:
            reachable = Path(self.settings.upload_dir).is_dir()
        except OSError:
            reachable = False
        return StorageHealth(
            backend=DEFAULT_STORAGE_BACKEND, bucket=None, reachable=reachable
        )


async def integrity_report(
    *,
    storage: StorageBackend,
    storage_key: str,
    expected_sha256: str,
    expected_size: int,
    algorithm: str,
) -> IntegrityResult:
    """Recompute the stored object's digest and compare it with the recorded one.

    A missing stored object is not an error: it is exactly the untrusted state this
    report exists to describe, so it answers verified=False with no recomputed digest.
    """
    recomputed = await storage.digest(key=storage_key, algorithm=algorithm)
    if recomputed is None:
        return IntegrityResult(
            sha256=expected_sha256,
            size=expected_size,
            verified=False,
            recomputed_sha256=None,
            recomputed_size=None,
        )

    return IntegrityResult(
        sha256=expected_sha256,
        size=expected_size,
        verified=secrets.compare_digest(recomputed.sha256, expected_sha256),
        recomputed_sha256=recomputed.sha256,
        recomputed_size=recomputed.size,
    )


def s3_usable(settings: MediaSettings) -> bool:
    """Whether the S3 knobs are complete enough to select the S3 backend.

    A bucket alone is not enough: without a region or an endpoint the client cannot
    know where the bucket lives, so that too falls back to the local backend.
    """
    return (
        settings.storage_backend == S3_STORAGE_BACKEND
        and bool(settings.s3_bucket.strip())
        and bool(settings.s3_region.strip() or settings.s3_endpoint_url.strip())
    )


def build_storage_backend(settings: MediaSettings) -> StorageBackend:
    """The backend for these settings; anything unusable falls back to local.

    `MEDIA_STORAGE_BACKEND=s3` selects the S3 backend only when the bucket and a region
    or endpoint are configured and the client can actually be built. Every other case --
    an unknown backend name, an incomplete S3 configuration, a client boto3 refuses to
    construct -- warns and answers the local backend, which is the same
    fall-back-not-raise convention the settings themselves follow.
    """
    if settings.storage_backend == S3_STORAGE_BACKEND:
        if not s3_usable(settings):
            logger.warning(
                "MEDIA_STORAGE_BACKEND %r needs S3_BUCKET and one of S3_REGION or "
                "S3_ENDPOINT_URL, using %r",
                settings.storage_backend,
                DEFAULT_STORAGE_BACKEND,
            )
            return LocalStorageBackend(settings)
        try:
            # Imported here so importing app.media never requires the SDK, and so a
            # missing or unusable one falls back instead of breaking boot.
            from .media_s3 import S3StorageBackend

            return S3StorageBackend(settings)
        except Exception as exc:
            logger.warning(
                "s3 media backend could not be built, using %r: %s",
                DEFAULT_STORAGE_BACKEND,
                exc,
            )
            return LocalStorageBackend(settings)

    if settings.storage_backend != DEFAULT_STORAGE_BACKEND:
        logger.warning(
            "unknown MEDIA_STORAGE_BACKEND %r, using %r",
            settings.storage_backend,
            DEFAULT_STORAGE_BACKEND,
        )
    return LocalStorageBackend(settings)


def get_media_settings(request: Request) -> MediaSettings:
    """The settings `create_app` put on app.state, so the environment is read once."""
    return request.app.state.media_settings


def get_storage_backend(request: Request) -> StorageBackend:
    """The backend for this app's own media settings; the router depends on this."""
    return build_storage_backend(get_media_settings(request))
