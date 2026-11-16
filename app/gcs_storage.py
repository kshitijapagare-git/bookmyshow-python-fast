"""The Google Cloud Storage backing for media storage, behind the `StorageBackend` seam.

`GcsStorageBackend` implements exactly the members `StorageBackend` declares, so the
upload, download, trailer, integrity and presign routes reach a bucket through the calls
they already make for a local directory or an S3 bucket. Object keys keep the local
backend's relative convention -- `{subdir}/{hash}{ext}`, or `{subdir}/{assetId}{ext}` for
a direct upload whose bytes have not been hashed yet -- and `GCS_MEDIA_PREFIX` is
prepended only on the wire, so `media_assets.storage_key` holds the same kind of value on
every backend.

`google-cloud-storage` is a hard dependency and is imported at module level, so a host
running this build always has it; the transport client is still injectable, through
`GcsStorageBackend(client=...)`, because the whole test suite runs against a hand-written
fake and needs neither credentials nor a network. `build_gcs_client` resolves credentials
from the two knobs the ticket names, with the raw `GCS_CREDENTIALS_JSON` document winning
over the `GOOGLE_APPLICATION_CREDENTIALS` path, and leaves application-default credentials
to the library when neither is set; it is memoised on the (frozen, hashable) settings
because the backend itself is built per request.

Writes go through `upload_from_file(..., rewind=True)`, reads stream through
`blob.open("rb", chunk_size=...)` -- or a single inclusive `download_as_bytes(start=,
end=)` for a ranged request -- and `public_url` regenerates a short-lived signed GET url on
every read instead of the row storing one that would expire. Signing needs a service
account with a private key, so both url producers are best-effort: when the resolved
credentials cannot sign, they log a warning and fall back to the token-signed upload route
and to `/media/{key}`. Note that `create_app` mounts `/media` only for the local backend,
so on a GCS deployment that fallback read url is not served -- the guaranteed read path is
`GET /api/media/{asset_id}`, which streams the object back through `open_stream` and never
needs a signature.

A client error is inspected for the library's own `NotFound` and, failing that, through a
`code`/`status` of 404, so an object that is simply gone is never an error.
"""

import json
import logging
import tempfile
from collections.abc import Iterator
from datetime import timedelta
from functools import lru_cache
from typing import Any

from fastapi import UploadFile
from google.api_core.exceptions import NotFound
from google.cloud import storage
from google.oauth2 import service_account

from .media import (
    ALLOWED_CONTENT_TYPES,
    CHUNK_SIZE,
    GCS_STORAGE_BACKEND,
    KIND_SUBDIRS,
    MEDIA_URL_PREFIX,
    ChunkSource,
    EmptyUploadError,
    ExactSizeMismatchError,
    MediaSettings,
    MediaTooLargeError,
    ObjectHead,
    PresignedUpload,
    StorageHealth,
    StoredObject,
    UnsupportedMediaTypeError,
    new_hasher,
    object_key,
)
from .schemas import MediaKind
from .streaming import ByteRange

logger = logging.getLogger(__name__)

GCS_READ_CHUNK_SIZE = 64 * 1024
GCS_SIGNING_VERSION = "v4"
NOT_FOUND_STATUS = 404


def resolve_gcs_credentials(settings: MediaSettings) -> Any | None:
    """The service-account credentials these settings name, or None when they name none.

    The raw `GCS_CREDENTIALS_JSON` document wins over the `GOOGLE_APPLICATION_CREDENTIALS`
    path, because it is the credential itself rather than a pointer to one. An unusable
    document or path raises, which is what lets `build_storage_backend` fall back to the
    local backend instead of failing every upload.
    """
    if settings.gcs_credentials_json:
        info = json.loads(settings.gcs_credentials_json)
        return service_account.Credentials.from_service_account_info(info)
    if settings.gcs_credentials_path:
        return service_account.Credentials.from_service_account_file(
            settings.gcs_credentials_path
        )
    return None


@lru_cache(maxsize=16)
def build_gcs_client(settings: MediaSettings) -> Any:
    """The bucket client for these settings, memoised on the frozen settings.

    The project is passed only when `GCS_PROJECT` names one, so a blank project is left
    to whatever the resolved credentials say, and the credentials are passed only when
    one of the two knobs produced them, leaving the library's own application-default
    lookup to answer otherwise. A client error is raised, never swallowed here: the
    caller is the one that knows how to fall back.
    """
    credentials = resolve_gcs_credentials(settings)
    options: dict[str, Any] = {}
    if credentials is not None:
        options["credentials"] = credentials
    if settings.gcs_project:
        options["project"] = settings.gcs_project
    return storage.Client(**options)


def _is_not_found(error: BaseException) -> bool:
    """Whether the client's error says the object is simply not there."""
    if isinstance(error, NotFound):
        return True
    code = getattr(error, "code", None)
    if callable(code):
        code = code()
    if isinstance(code, int) and code == NOT_FOUND_STATUS:
        return True
    if isinstance(code, str) and code.isdigit() and int(code) == NOT_FOUND_STATUS:
        return True
    status_code = getattr(error, "status", None)
    if isinstance(status_code, int) and status_code == NOT_FOUND_STATUS:
        return True
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        metadata = response.get("ResponseMetadata")
        if isinstance(metadata, dict):
            return metadata.get("HTTPStatusCode") == NOT_FOUND_STATUS
    return False


class GcsStorageBackend:
    """Stores media objects in one Google Cloud Storage bucket, under the configured prefix."""

    def __init__(self, settings: MediaSettings, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def client(self) -> Any:
        """The injected client, or the real one built from the GCS_* settings on first use."""
        if self._client is None:
            self._client = build_gcs_client(self.settings)
        return self._client

    def _wire_key(self, key: str) -> str:
        """The bucket key for a backend-relative key: the prefix plus a single '/'."""
        prefix = self.settings.gcs_media_prefix
        return f"{prefix}/{key}" if prefix else key

    def _bucket(self) -> Any:
        """The configured bucket handle."""
        return self.client.bucket(self.settings.gcs_bucket)

    def _blob(self, key: str) -> Any:
        """The blob handle for a backend-relative key, with the prefix on the wire."""
        return self._bucket().blob(self._wire_key(key))

    def _put(self, *, key: str, content_type: str, body: Any) -> None:
        """Upload one object from the start of `body`, recording its content type."""
        self._blob(key).upload_from_file(
            body, content_type=content_type, rewind=True
        )

    def _signed_url(
        self, *, key: str, method: str, content_type: str | None = None
    ) -> str | None:
        """A V4 signed url for this key, or None when the credentials cannot sign.

        Signing needs a service account with a private key, so a keyless identity -- a
        compute-engine default, say -- raises here; that is a deployment possibility
        rather than a bug, so it is logged and answered as None and the caller falls
        back to the token-signed upload route or the `/media` url.
        """
        options: dict[str, Any] = {
            "version": GCS_SIGNING_VERSION,
            "expiration": timedelta(seconds=self.settings.presign_expires_seconds),
            "method": method,
        }
        if content_type is not None:
            options["content_type"] = content_type
        try:
            return self._blob(key).generate_signed_url(**options)
        except Exception as error:  # a keyless identity is a state, not a failure
            logger.warning(
                "gcs media bucket %r cannot sign a %s url for %r: %s",
                self.settings.gcs_bucket,
                method,
                key,
                error,
            )
            return None

    async def save(
        self,
        *,
        kind: MediaKind,
        content_type: str,
        upload: UploadFile,
        max_bytes: int,
    ) -> StoredObject:
        """Spool the stream while hashing it, then upload it under its sha-derived key.

        Nothing reaches the bucket until the whole body has been read, so an empty or
        oversized upload leaves no object behind at all.
        """
        digest = new_hasher(self.settings.hash_algorithm)
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=CHUNK_SIZE) as spool:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise MediaTooLargeError(max_bytes)
                digest.update(chunk)
                spool.write(chunk)
            if size == 0:
                raise EmptyUploadError()

            sha256 = digest.hexdigest()
            key = object_key(kind=kind, content_type=content_type, sha256=sha256)
            spool.seek(0)
            self._put(key=key, content_type=content_type, body=spool)

        return StoredObject(key=key, size=size, sha256=sha256)

    async def save_exact(
        self,
        *,
        kind: MediaKind,
        content_type: str,
        upload: ChunkSource,
        expected_size: int,
    ) -> StoredObject:
        """Spool the body, and upload it only when it is exactly the declared size."""
        digest = new_hasher(self.settings.hash_algorithm)
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=CHUNK_SIZE) as spool:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
                spool.write(chunk)
            if size != expected_size:
                raise ExactSizeMismatchError(expected=expected_size, actual=size)

            sha256 = digest.hexdigest()
            key = object_key(kind=kind, content_type=content_type, sha256=sha256)
            spool.seek(0)
            self._put(key=key, content_type=content_type, body=spool)

        return StoredObject(key=key, size=size, sha256=sha256)

    def open_stream(
        self, key: str, byte_range: ByteRange | None = None
    ) -> Iterator[bytes]:
        """Stream the object out of the bucket, honouring one `bytes=start-end` slice."""
        blob = self._blob(key)
        if byte_range is not None:
            # `start`/`end` are inclusive, exactly as `ByteRange` is.
            payload = blob.download_as_bytes(
                start=byte_range.start, end=byte_range.end
            )
            for start in range(0, len(payload), GCS_READ_CHUNK_SIZE):
                yield payload[start : start + GCS_READ_CHUNK_SIZE]
            return

        reader = blob.open("rb", chunk_size=GCS_READ_CHUNK_SIZE)
        try:
            while True:
                chunk = reader.read(GCS_READ_CHUNK_SIZE)
                if not chunk:
                    return
                yield chunk
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()

    def read_bytes(self, key: str) -> bytes:
        """The whole object; only for objects already known to be small."""
        return self._blob(key).download_as_bytes()

    def delete(self, key: str) -> None:
        """Delete the object; one that is already gone is not an error."""
        try:
            self._blob(key).delete()
        except Exception as error:
            if _is_not_found(error):
                return
            raise

    def head(self, key: str) -> ObjectHead | None:
        """The object's size and content type without reading it; None when absent."""
        blob = self._blob(key)
        try:
            blob.reload()
        except Exception as error:
            if _is_not_found(error):
                return None
            raise
        return ObjectHead(
            size=int(blob.size or 0),
            content_type=blob.content_type,
        )

    def exists(self, key: str) -> bool:
        """Whether an object is stored under this key right now."""
        return self.head(key) is not None

    def public_url(self, key: str) -> str:
        """A freshly signed GET url, because the objects themselves stay private.

        The objects are never public, so a signed url is the read path; when the
        credentials cannot sign, `/media/{key}` is answered rather than raised, which is
        what a local-backend deployment would have served.
        """
        signed = self._signed_url(key=key, method="GET")
        if signed is not None:
            return signed
        return f"{MEDIA_URL_PREFIX}/{key}"

    def presigned_upload(
        self,
        *,
        asset_id: int,
        kind: MediaKind,
        size: int,
        content_type: str,
    ) -> PresignedUpload:
        """The signed `PUT` the browser sends the bytes to, and the key they land under.

        The destination is derived from the asset id rather than its digest, because the
        digest is not known before the bytes arrive. When the credentials cannot sign,
        the token-signed route this app already serves is answered instead, with an empty
        key, exactly as the local backend does.
        """
        extension = ALLOWED_CONTENT_TYPES.get(kind, {}).get(content_type)
        if extension is None:
            raise UnsupportedMediaTypeError(content_type)

        key = f"{KIND_SUBDIRS[kind]}/{asset_id}{extension}"
        headers = {"Content-Type": content_type}
        signed = self._signed_url(
            key=key, method="PUT", content_type=content_type
        )
        if signed is not None:
            return PresignedUpload(
                url=signed, method="PUT", headers=headers, key=key
            )

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
            headers=headers,
            key="",
        )

    def health(self) -> StorageHealth:
        """Whether the bucket answers right now; never raises, it reports instead."""
        if not self.settings.gcs_bucket:
            return StorageHealth(backend=GCS_STORAGE_BACKEND, reachable=False)
        try:
            reachable = bool(self._bucket().exists())
        except Exception as error:  # a probe must report down, never fail the route
            logger.warning(
                "gcs media bucket %r is unreachable: %s",
                self.settings.gcs_bucket,
                error,
            )
            return StorageHealth(backend=GCS_STORAGE_BACKEND, reachable=False)
        return StorageHealth(backend=GCS_STORAGE_BACKEND, reachable=reachable)
