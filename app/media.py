"""Storage for a movie's poster, backdrop and trailer uploads.

The bytes go through one `StorageBackend`, so the router never touches a filesystem
or an SDK directly: `LocalStorageBackend` streams the upload in `CHUNK_SIZE` chunks
into a `.part` file inside the destination directory, hashing the bytes with SHA-256
as they go, and only then `os.replace`s the temp file into its final, content-addressed
key. Saving the same bytes twice therefore lands on the same key, and an oversized or
empty upload leaves nothing behind at all.

`MediaSettings.from_env()` reads the knobs with the same fall-back-not-raise convention
as `PaymentsSettings` and `ReviewsSettings`: an absent, blank or unparseable value
selects the default instead of raising. `MEDIA_HASH_ALGORITHM` is honoured the same way,
but only `sha256` is supported, so anything else falls back to it.

`recompute_digest` and `integrity_for` are the one place stored bytes are re-hashed: the
integrity and verify routes and the review-photo join all read their digest through
them, so a file whose bytes changed after upload reports `verified=False` rather than
agreeing with the digest recorded on its row.
"""

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastapi import Request, UploadFile

from .schemas import MediaKind

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_BACKEND = "local"
DEFAULT_UPLOAD_DIR = "media"
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_VIDEO_BYTES = 100 * 1024 * 1024
DEFAULT_MEDIA_SERVICE_TOKEN = "dev-service-token"
DEFAULT_HASH_ALGORITHM = "sha256"
SUPPORTED_HASH_ALGORITHMS: tuple[str, ...] = (DEFAULT_HASH_ALGORITHM,)

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


def _hash_algorithm() -> str:
    """A supported digest name from the environment; anything else falls back."""
    raw = _env("MEDIA_HASH_ALGORITHM", "").lower()
    if not raw:
        return DEFAULT_HASH_ALGORITHM
    if raw in SUPPORTED_HASH_ALGORITHMS:
        return raw
    logger.warning(
        "unsupported MEDIA_HASH_ALGORITHM %r, using %r",
        raw,
        DEFAULT_HASH_ALGORITHM,
    )
    return DEFAULT_HASH_ALGORITHM


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
            hash_algorithm=_hash_algorithm(),
        )


def max_bytes_for(kind: MediaKind, settings: MediaSettings) -> int:
    """The configured byte limit: the image one for images, else the video one."""
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


@dataclass(frozen=True)
class AssetIntegrity:
    """An asset's recorded digest and size, plus whether storage still matches them."""

    sha256: str
    size: int
    verified: bool


def recompute_digest(
    path: Path, *, algorithm: str = DEFAULT_HASH_ALGORITHM
) -> str | None:
    """Hash the bytes now stored at `path`; None when there is no file to read."""
    if not path.is_file():
        return None
    digest = hashlib.new(algorithm)
    with path.open("rb") as source:
        while True:
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def integrity_for(
    *,
    storage: StorageBackend,
    storage_key: str,
    recorded_sha256: str,
    size_bytes: int,
    algorithm: str = DEFAULT_HASH_ALGORITHM,
) -> AssetIntegrity:
    """Compare the row's recorded digest with the digest of the stored bytes.

    The digest and size reported are the ones recorded on the row; `verified` is the
    only recomputed answer, so a changed or missing file reads as untrusted while the
    metadata it was uploaded with stays visible.
    """
    recomputed = recompute_digest(
        storage.path_for(storage_key), algorithm=algorithm
    )
    return AssetIntegrity(
        sha256=recorded_sha256,
        size=size_bytes,
        verified=recomputed is not None and recomputed == recorded_sha256,
    )


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

        digest = hashlib.new(self.settings.hash_algorithm)
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
