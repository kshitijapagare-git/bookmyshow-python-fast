"""Storage for uploaded media: movie posters, backdrops, trailers and review photos.

The bytes go through one `StorageBackend`, so the router never touches a filesystem
or an SDK directly: `LocalStorageBackend` streams the upload in `CHUNK_SIZE` chunks
into a `.part` file inside the destination directory, hashing the bytes with SHA-256
as they go, and only then `os.replace`s the temp file into its final, content-addressed
key. Saving the same bytes twice therefore lands on the same key, and an oversized or
empty upload leaves nothing behind at all.

`MediaSettings.from_env()` reads the six knobs -- the hash algorithm included -- with the
same fall-back-not-raise convention as `PaymentsSettings` and `ReviewsSettings`: an
absent, blank or unparseable value selects the default instead of raising.

`integrity_report` is the read-back seam for the integrity and verify routes: it streams
the stored object through `digest_of_file` and compares the result in constant time with
the digest recorded at upload, so a file replaced on disk reads as untrusted.
"""

import hashlib
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fastapi import Request, UploadFile

from .schemas import MediaKind

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_BACKEND = "local"
DEFAULT_UPLOAD_DIR = "media"
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_VIDEO_BYTES = 100 * 1024 * 1024
DEFAULT_MEDIA_SERVICE_TOKEN = "dev-service-token"
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
}

IMAGE_KINDS: tuple[MediaKind, ...] = (
    MediaKind.POSTER,
    MediaKind.BACKDROP,
    MediaKind.REVIEW_PHOTO,
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

    @classmethod
    def from_env(cls) -> "MediaSettings":
        """The configured settings; anything unrecognised falls back, never raises."""
        configured = _env("MEDIA_HASH_ALGORITHM", "").lower()
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


class StorageBackend(Protocol):
    """The one seam between the upload route and wherever the bytes actually live."""

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

    def public_url(self, key: str) -> str:
        """The public path the stored object is served from."""
        ...

    def path_for(self, key: str) -> Path:
        """Where this key lives on local disk, for the backends that have a disk."""
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

    async def save(
        self,
        *,
        kind: MediaKind,
        content_type: str,
        upload: UploadFile,
        max_bytes: int,
    ) -> StoredObject:
        """Copy the stream to a temp file, then move it onto its sha-derived key."""
        destination_dir = Path(self.settings.upload_dir) / KIND_SUBDIRS[kind]
        destination_dir.mkdir(parents=True, exist_ok=True)
        part_path = destination_dir / f"{uuid.uuid4().hex}{PART_SUFFIX}"

        digest = new_hasher(self.settings.hash_algorithm)
        size = 0
        try:
            with part_path.open("wb") as sink:
                while True:
                    chunk = await upload.read(CHUNK_SIZE)
                    if not chunk:
                        break
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


def integrity_report(
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
    path = storage.path_for(storage_key)
    if not path.is_file():
        return IntegrityResult(
            sha256=expected_sha256,
            size=expected_size,
            verified=False,
            recomputed_sha256=None,
            recomputed_size=None,
        )

    recomputed_sha256, recomputed_size = digest_of_file(path=path, algorithm=algorithm)
    return IntegrityResult(
        sha256=expected_sha256,
        size=expected_size,
        verified=secrets.compare_digest(recomputed_sha256, expected_sha256),
        recomputed_sha256=recomputed_sha256,
        recomputed_size=recomputed_size,
    )


def build_storage_backend(settings: MediaSettings) -> StorageBackend:
    """The backend for these settings; an unknown backend name falls back to local."""
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
