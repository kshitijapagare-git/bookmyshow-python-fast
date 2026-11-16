"""Storage for uploaded media: movie posters, backdrops, trailers and review photos.

The bytes go through one `StorageBackend`, so the router never touches a filesystem
or an SDK directly: `LocalStorageBackend` streams the upload in `CHUNK_SIZE` chunks
into a `.part` file inside the destination directory, hashing the bytes with SHA-256
as they go, and only then `os.replace`s the temp file into its final, content-addressed
key. Saving the same bytes twice therefore lands on the same key, and an oversized or
empty upload leaves nothing behind at all.

`save_exact` is the direct-upload path: the same temp-file mechanics, but it stores
the body only when its byte count is exactly the size the client declared at presign.

`MediaSettings.from_env()` reads the configured knobs -- the hash algorithm and the
presign secret and lifetime included -- with the same fall-back-not-raise convention as
`PaymentsSettings` and `ReviewsSettings`: an absent, blank or unparseable value selects
the default instead of raising.

The seam reads back through the same interface it writes through -- `exists`,
`head`, `open_stream`, `read_bytes` and `delete` -- so a route never needs to know
whether an object is a file on this host or an object in a bucket; only
`LocalStorageBackend` still has a `path_for`. `integrity_report` is the read-back seam
for the integrity and verify routes: it streams the stored object through
`digest_of_stream` and compares the result in constant time with the digest recorded at
upload, so an object replaced after the upload reads as untrusted.
"""

import hashlib
import logging
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fastapi import Request, UploadFile

from .schemas import MediaKind
from .streaming import ByteRange, iter_file_range

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_BACKEND = "local"
# The backend names this app knows; every other name falls back to local.
S3_STORAGE_BACKEND = "s3"
GCS_STORAGE_BACKEND = "gcs"
MINIO_STORAGE_BACKEND = "minio"
DEFAULT_UPLOAD_DIR = "media"
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_VIDEO_BYTES = 100 * 1024 * 1024
DEFAULT_MEDIA_SERVICE_TOKEN = "dev-service-token"
DEFAULT_HASH_ALGORITHM = "sha256"
SUPPORTED_HASH_ALGORITHMS: frozenset[str] = frozenset({DEFAULT_HASH_ALGORITHM})
DEFAULT_MEDIA_PRESIGN_EXPIRES_SECONDS = 900
DEFAULT_MEDIA_PRESIGN_SECRET = "dev-presign-secret-change-me"
DEFAULT_S3_REGION = "us-east-1"
# An empty prefix keeps every object key at the bucket root.
DEFAULT_S3_MEDIA_PREFIX = ""
DEFAULT_GCS_MEDIA_PREFIX = ""
DEFAULT_MINIO_MEDIA_PREFIX = ""
# Secure by default, because a MinIO reached over https needs no extra knob; a local
# container sets MINIO_SECURE=false.
DEFAULT_MINIO_SECURE = True
# Creating a bucket is never implicit: a deployment that needs it opts in.
DEFAULT_MINIO_CREATE_BUCKET = False
SUPPORTED_SSE: frozenset[str] = frozenset({"AES256"})
# The spellings a boolean knob accepts, so a value never has to be a Python literal.
TRUE_FLAGS: frozenset[str] = frozenset({"true", "1", "yes", "on"})
FALSE_FLAGS: frozenset[str] = frozenset({"false", "0", "no", "off"})

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


def _strip_slashes(value: str) -> str:
    """A key prefix without the leading and trailing slashes joining would double."""
    return (value or "").strip().strip("/")


def _sse(value: str) -> str | None:
    """The configured server-side encryption, or None when it is off or unsupported.

    Only the algorithms S3 actually accepts are kept, so a mistyped `S3_SSE` turns
    encryption off with a warning rather than failing every put with a parameter error.
    """
    configured = (value or "").strip().upper()
    if not configured:
        return None
    if configured not in SUPPORTED_SSE:
        logger.warning("unsupported S3_SSE %r, storing with the bucket default", value)
        return None
    return configured


def _flag(name: str, default: bool) -> bool:
    """A boolean from the environment; a blank or unrecognised value keeps the default.

    Only the spellings a human actually writes are accepted, case-insensitively, so a
    mistyped `MINIO_SECURE` falls back instead of turning `http` into `https` by
    accident -- exactly the convention `_sse` uses for its own knob.
    """
    raw = _env(name, "").lower()
    if not raw:
        return default
    if raw in TRUE_FLAGS:
        return True
    if raw in FALSE_FLAGS:
        return False
    logger.warning("unrecognised %s %r, using %r", name, raw, default)
    return default


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


def digest_of_stream(
    stream: Iterable[bytes], *, algorithm: str, chunk_size: int = CHUNK_SIZE
) -> tuple[str, int]:
    """Hash an already-chunked object, answering its hex digest and byte count.

    The chunks come from `StorageBackend.open_stream`, so this works the same for a
    file on disk and for an object read out of a bucket, and the object is never held
    whole in memory to hash it.
    """
    digest = new_hasher(algorithm)
    size = 0
    for chunk in stream:
        if not chunk:
            continue
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
    presign_secret: str = DEFAULT_MEDIA_PRESIGN_SECRET
    presign_expires_seconds: int = DEFAULT_MEDIA_PRESIGN_EXPIRES_SECONDS
    # Where the S3 backend stores objects; a blank bucket means S3 is not configured.
    s3_bucket: str = ""
    s3_region: str = DEFAULT_S3_REGION
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_media_prefix: str = DEFAULT_S3_MEDIA_PREFIX
    s3_sse: str | None = None
    # Where the GCS backend stores objects; a blank bucket means GCS is not configured.
    gcs_bucket: str = ""
    # A blank project leaves it to whatever the resolved credentials name.
    gcs_project: str = ""
    # The raw service-account document, and its fallback path; the JSON wins when set.
    gcs_credentials_json: str = ""
    gcs_credentials_path: str = ""
    gcs_media_prefix: str = DEFAULT_GCS_MEDIA_PREFIX
    # Where the MinIO backend stores objects; a blank bucket means MinIO is not configured.
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = ""
    minio_secure: bool = DEFAULT_MINIO_SECURE
    minio_media_prefix: str = DEFAULT_MINIO_MEDIA_PREFIX
    minio_create_bucket: bool = DEFAULT_MINIO_CREATE_BUCKET

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
            presign_secret=_env("MEDIA_PRESIGN_SECRET", DEFAULT_MEDIA_PRESIGN_SECRET),
            presign_expires_seconds=_positive_int(
                "MEDIA_PRESIGN_EXPIRES_SECONDS", DEFAULT_MEDIA_PRESIGN_EXPIRES_SECONDS
            ),
            s3_bucket=_env("S3_BUCKET", ""),
            s3_region=_env("S3_REGION", DEFAULT_S3_REGION),
            s3_endpoint_url=_env("S3_ENDPOINT_URL", ""),
            s3_access_key_id=_env("S3_ACCESS_KEY_ID", ""),
            s3_secret_access_key=_env("S3_SECRET_ACCESS_KEY", ""),
            s3_media_prefix=_strip_slashes(
                _env("S3_MEDIA_PREFIX", DEFAULT_S3_MEDIA_PREFIX)
            ),
            s3_sse=_sse(_env("S3_SSE", "")),
            gcs_bucket=_env("GCS_BUCKET", ""),
            gcs_project=_env("GCS_PROJECT", ""),
            gcs_credentials_json=_env("GCS_CREDENTIALS_JSON", ""),
            gcs_credentials_path=_env("GOOGLE_APPLICATION_CREDENTIALS", ""),
            gcs_media_prefix=_strip_slashes(
                _env("GCS_MEDIA_PREFIX", DEFAULT_GCS_MEDIA_PREFIX)
            ),
            minio_endpoint=_env("MINIO_ENDPOINT", ""),
            minio_access_key=_env("MINIO_ACCESS_KEY", ""),
            minio_secret_key=_env("MINIO_SECRET_KEY", ""),
            minio_bucket=_env("MINIO_BUCKET", ""),
            minio_secure=_flag("MINIO_SECURE", DEFAULT_MINIO_SECURE),
            minio_media_prefix=_strip_slashes(
                _env("MINIO_MEDIA_PREFIX", DEFAULT_MINIO_MEDIA_PREFIX)
            ),
            minio_create_bucket=_flag(
                "MINIO_CREATE_BUCKET", DEFAULT_MINIO_CREATE_BUCKET
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


class ExactSizeMismatchError(Exception):
    """The body was not exactly the size declared at presign; the .part file is removed."""

    def __init__(self, *, expected: int, actual: int) -> None:
        super().__init__(
            f"upload is {actual} bytes but {expected} bytes were declared"
        )
        self.expected = expected
        self.actual = actual


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
class PresignedUpload:
    """Where a client uploads its bytes: the url, its method and its headers.

    `key` is the destination the backend will store the bytes under, or "" when the
    backend derives it from the bytes themselves -- the local backend, which only sees
    them once they have been streamed through its own upload route.
    """

    url: str
    method: str
    headers: dict[str, str]
    key: str


@dataclass(frozen=True)
class ObjectHead:
    """What a backend can report about a stored object without reading its bytes."""

    size: int
    content_type: str | None


@dataclass(frozen=True)
class StorageHealth:
    """Whether the backend's storage answers right now, and which backend it is.

    `bucket` and `endpoint` name where a bucket-backed backend is pointed; they stay
    empty for the local, S3 and GCS backends, so a caller that only cares about the
    backend and its reachability keeps comparing the same two values it always did.
    """

    backend: str
    reachable: bool
    bucket: str = ""
    endpoint: str = ""


@dataclass(frozen=True)
class IntegrityResult:
    """One integrity read: the values recorded at upload and the ones recomputed now.

    `sha256` and `size` are what the `MediaAsset` row says it stored; `recomputed_*`
    are what the bytes in storage hash to right now, and are None when the object is gone.
    """

    sha256: str
    size: int
    verified: bool
    recomputed_sha256: str | None
    recomputed_size: int | None


def object_key(*, kind: MediaKind, content_type: str, sha256: str) -> str:
    """`{subdir}/{sha256}{ext}`: the posting of identical bytes twice is one file."""
    return f"{KIND_SUBDIRS[kind]}/{sha256}{ALLOWED_CONTENT_TYPES[kind][content_type]}"


class ChunkSource(Protocol):
    """The `read(size)` shape both save methods stream a body through."""

    async def read(self, size: int = -1) -> bytes:
        """The next slice of the body, or b"" once it is exhausted."""
        ...


class RequestStreamChunks:
    """Adapts a request body iterator to the `read(size)` shape storage expects.

    Only the unread remainder of the chunk already pulled is held, so a long body is
    handed to storage in the chunks the server received it in rather than buffered
    whole. A read answers at most the current remainder, as a stream may.
    """

    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._stream = stream
        self._buffer = b""
        self._exhausted = False

    async def _fill(self) -> None:
        """Pull the next chunk when nothing is buffered, remembering the end."""
        while not self._exhausted and not self._buffer:
            try:
                self._buffer = await self._stream.__anext__()
            except StopAsyncIteration:
                self._exhausted = True
                self._buffer = b""

    async def read(self, size: int = -1) -> bytes:
        """The next slice of the body, or b"" once it is exhausted."""
        await self._fill()
        if not self._buffer:
            return b""
        if size is None or size < 0:
            chunk = self._buffer
            self._buffer = b""
            return chunk
        chunk = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return chunk


class StorageBackend(Protocol):
    """The one seam between the upload routes and wherever the bytes actually live.

    Writing goes through `save`/`save_exact`, reading through `exists`/`head`/
    `open_stream`/`read_bytes`, and `presigned_upload`/`public_url` are how a client is
    handed a url to upload to or to load. Only the local backend has a `path_for`; a
    route that needs one is a route that has not been fitted to the seam.
    """

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

    async def save_exact(
        self,
        *,
        kind: MediaKind,
        content_type: str,
        upload: ChunkSource,
        expected_size: int,
    ) -> StoredObject:
        """Store `upload` only when it is exactly `expected_size` bytes long."""
        ...

    def public_url(self, key: str) -> str:
        """The url a client loads the stored object from."""
        ...

    def open_stream(
        self, key: str, byte_range: ByteRange | None = None
    ) -> Iterator[bytes]:
        """Yield the stored object in bounded chunks, honouring one byte range."""
        ...

    def read_bytes(self, key: str) -> bytes:
        """The whole object's bytes; only for objects already known to be small."""
        ...

    def delete(self, key: str) -> None:
        """Remove the stored object; deleting one that is already gone is not an error."""
        ...

    def exists(self, key: str) -> bool:
        """Whether an object is stored under this key right now."""
        ...

    def head(self, key: str) -> ObjectHead | None:
        """What storage reports about the object without reading it; None when absent."""
        ...

    def presigned_upload(
        self,
        *,
        asset_id: int,
        kind: MediaKind,
        size: int,
        content_type: str,
    ) -> PresignedUpload:
        """Where a client uploads the declared bytes, and the key they land under."""
        ...

    def health(self) -> StorageHealth:
        """Whether this backend's storage answers; never raises, it reports instead."""
        ...


class LocalStorageBackend:
    """Streams uploads onto local disk, content-addressed by their SHA-256."""

    def __init__(self, settings: MediaSettings) -> None:
        self.settings = settings

    def path_for(self, key: str) -> Path:
        """`MEDIA_UPLOAD_DIR` joined with the key; the key is built here, never by callers.

        This is a local-backend detail rather than part of the storage seam: everything
        outside this class reads through `open_stream`, `read_bytes` or `head`.
        """
        return Path(self.settings.upload_dir) / key

    def public_url(self, key: str) -> str:
        """`/media/{key}`, served by the static mount `create_app` installs."""
        return f"{MEDIA_URL_PREFIX}/{key}"

    def open_stream(
        self, key: str, byte_range: ByteRange | None = None
    ) -> Iterator[bytes]:
        """Stream the stored file through `iter_file_range`, at most one chunk a read."""
        return iter_file_range(self.path_for(key), byte_range)

    def read_bytes(self, key: str) -> bytes:
        """The whole stored file; only for objects already known to be small."""
        return self.path_for(key).read_bytes()

    def delete(self, key: str) -> None:
        """Unlink the stored file; a key with no file behind it is not an error."""
        self.path_for(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        """Whether the file this key names is present on disk."""
        return self.path_for(key).is_file()

    def head(self, key: str) -> ObjectHead | None:
        """The stored file's size; None when nothing is stored under the key.

        A plain file carries no content type of its own -- the row records what the
        upload declared -- so the reported content type is always None here.
        """
        path = self.path_for(key)
        if not path.is_file():
            return None
        return ObjectHead(size=path.stat().st_size, content_type=None)

    def presigned_upload(
        self,
        *,
        asset_id: int,
        kind: MediaKind,
        size: int,
        content_type: str,
    ) -> PresignedUpload:
        """The token-signed `PUT` this backend's own upload route accepts.

        The token lives in `app.presign`, which imports `MediaSettings` from this
        module, so it is imported inside the method rather than at module level. The
        destination key is empty because `save_exact` derives it from the bytes.
        """
        from .presign import upload_url

        return PresignedUpload(
            url=upload_url(
                self.settings,
                asset_id=asset_id,
                kind=kind,
                size=size,
                content_type=content_type,
            ),
            method="PUT",
            headers={"Content-Type": content_type},
            key="",
        )

    def health(self) -> StorageHealth:
        """Reachable exactly when the configured upload directory exists."""
        return StorageHealth(
            backend=DEFAULT_STORAGE_BACKEND,
            reachable=Path(self.settings.upload_dir).is_dir(),
        )

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

    async def save_exact(
        self,
        *,
        kind: MediaKind,
        content_type: str,
        upload: ChunkSource,
        expected_size: int,
    ) -> StoredObject:
        """Copy the body to a temp file and move it onto its key only at the exact size."""
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
                    digest.update(chunk)
                    sink.write(chunk)
            if size != expected_size:
                raise ExactSizeMismatchError(expected=expected_size, actual=size)

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

    The object is read back through the storage seam -- `exists` and `open_stream` --
    so the report works for a file on this host and for a bucket alike. A missing
    stored object is not an error: it is exactly the untrusted state this report exists
    to describe, so it answers verified=False with no recomputed digest.
    """
    if not storage.exists(storage_key):
        return IntegrityResult(
            sha256=expected_sha256,
            size=expected_size,
            verified=False,
            recomputed_sha256=None,
            recomputed_size=None,
        )

    recomputed_sha256, recomputed_size = digest_of_stream(
        storage.open_stream(storage_key), algorithm=algorithm
    )
    return IntegrityResult(
        sha256=expected_sha256,
        size=expected_size,
        verified=secrets.compare_digest(recomputed_sha256, expected_sha256),
        recomputed_sha256=recomputed_sha256,
        recomputed_size=recomputed_size,
    )


def build_storage_backend(settings: MediaSettings) -> StorageBackend:
    """The backend these settings select; anything unusable falls back to local.

    `MEDIA_STORAGE_BACKEND=s3` selects the bucket-backed backend once `S3_BUCKET` names
    a bucket. An s3 selection with no bucket is a configuration mistake rather than a
    usable backend -- it would fail every upload with a client parameter error -- so it
    warns and falls back to local, exactly as an unknown name does.
    `MEDIA_STORAGE_BACKEND=gcs` selects the Google Cloud Storage backend the same way,
    once `GCS_BUCKET` names a bucket, and falls back to local both when that bucket is
    blank and when the client or its credentials cannot be resolved -- an unusable
    configuration must be visible in the log rather than served silently from disk. The
    client is memoised on the settings, so resolving it eagerly here is a cache hit for
    every request after the first.
    `MEDIA_STORAGE_BACKEND=minio` selects the S3-compatible backend pointed at a MinIO
    endpoint, once `MINIO_BUCKET` names a bucket. A blank bucket -- and a blank
    `MINIO_ENDPOINT`, which would leave boto3 addressing AWS S3 instead of the local
    MinIO -- is a configuration mistake rather than a usable backend, so it warns and
    falls back to local exactly as the s3 selection without a bucket does.
    `app.s3_storage`, `app.gcs_storage` and `app.minio_storage` import the settings and
    helpers from this module, so they are imported inside this function rather than at
    module level.
    """
    if settings.storage_backend == S3_STORAGE_BACKEND:
        if not settings.s3_bucket:
            logger.warning(
                "MEDIA_STORAGE_BACKEND is %r but S3_BUCKET is not set, using %r",
                S3_STORAGE_BACKEND,
                DEFAULT_STORAGE_BACKEND,
            )
            return LocalStorageBackend(settings)
        from .s3_storage import S3StorageBackend

        return S3StorageBackend(settings)

    if settings.storage_backend == GCS_STORAGE_BACKEND:
        if not settings.gcs_bucket:
            logger.warning(
                "MEDIA_STORAGE_BACKEND is %r but GCS_BUCKET is not set, using %r",
                GCS_STORAGE_BACKEND,
                DEFAULT_STORAGE_BACKEND,
            )
            return LocalStorageBackend(settings)
        from .gcs_storage import GcsStorageBackend

        backend = GcsStorageBackend(settings)
        try:
            # Resolving the client is where an unusable credentials document or path
            # surfaces, and that is a deployment mistake rather than a usable backend.
            _ = backend.client
        except Exception as error:
            logger.warning(
                "MEDIA_STORAGE_BACKEND is %r but the GCS client for %r is unusable "
                "(%s), using %r",
                GCS_STORAGE_BACKEND,
                settings.gcs_bucket,
                error,
                DEFAULT_STORAGE_BACKEND,
            )
            return LocalStorageBackend(settings)
        return backend

    if settings.storage_backend == MINIO_STORAGE_BACKEND:
        if not settings.minio_bucket:
            logger.warning(
                "MEDIA_STORAGE_BACKEND is %r but MINIO_BUCKET is not set, using %r",
                MINIO_STORAGE_BACKEND,
                DEFAULT_STORAGE_BACKEND,
            )
            return LocalStorageBackend(settings)
        if not settings.minio_endpoint:
            logger.warning(
                "MEDIA_STORAGE_BACKEND is %r but MINIO_ENDPOINT is not set, using %r",
                MINIO_STORAGE_BACKEND,
                DEFAULT_STORAGE_BACKEND,
            )
            return LocalStorageBackend(settings)
        from .minio_storage import MinioStorageBackend

        return MinioStorageBackend(settings)

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
