"""S3 storage for uploaded media, behind the one `StorageBackend` seam.

This module owns every boto3 call, so no router and no domain code imports the SDK:
`build_s3_client` builds the one client from `MediaSettings`, and `S3StorageBackend`
implements the same interface `LocalStorageBackend` does, so a route never learns
whether the object lives on a local disk or in a bucket.

`boto3` is imported inside `build_s3_client`, never at module import time, so a host
that has not installed it -- or whose `S3_ENDPOINT_URL` is malformed -- still boots and
only the backend selection falls back to local.

An S3 key cannot be content-addressed, because the digest is only known once the bytes
have arrived, so `prepare_key` reserves `{prefix}/{subdir}/{uuid4().hex}{ext}` under
`S3_MEDIA_PREFIX` and `save_stream` writes the bytes to that same kind of key. Every
object is put with `ACL='private'`, and with `ServerSideEncryption='AES256'` when
`S3_SSE=AES256`. Uploads stream through a `SpooledTemporaryFile`, which rolls over to a
real temp file past `CHUNK_SIZE`, so a 100 MB trailer is never held in memory; the
digest is computed while the bytes are written, exactly as the local backend does it.
"""

import logging
import tempfile
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import UploadFile

from .media import (
    ALLOWED_CONTENT_TYPES,
    CHUNK_SIZE,
    DEFAULT_S3_MEDIA_PREFIX,
    KIND_SUBDIRS,
    MEDIA_URL_PREFIX,
    S3_SSE_AES256,
    DigestResult,
    EmptyUploadError,
    MediaSettings,
    MediaTooLargeError,
    PresignedUpload,
    StorageHealth,
    StoredObject,
    new_hasher,
)
from .schemas import MediaKind

logger = logging.getLogger(__name__)

S3_STORAGE_BACKEND_NAME = "s3"
S3_ACL_PRIVATE = "private"
S3_PRESIGNED_UPLOAD_OPERATION = "put_object"
S3_PRESIGNED_DOWNLOAD_OPERATION = "get_object"
# With a custom endpoint the bucket is a path segment, which is what S3-compatible
# servers and LocalStack answer; without one, AWS's virtual-hosted style is used.
S3_ADDRESSING_STYLE = "path"
# The client errors that mean "no object under this key", not "storage is broken".
S3_MISSING_OBJECT_CODES: frozenset[str] = frozenset(
    {"404", "NoSuchKey", "NotFound"}
)


def build_s3_client(settings: MediaSettings) -> Any:
    """Build the one boto3 S3 client for these settings; imported lazily on purpose.

    An empty `S3_ENDPOINT_URL` means real AWS, and an empty region or key pair lets
    boto3 resolve its own chain rather than being handed a blank value.
    """
    import boto3
    from botocore.config import Config

    options: dict[str, Any] = {
        "region_name": settings.s3_region or None,
        "endpoint_url": settings.s3_endpoint_url or None,
    }
    if settings.s3_access_key_id and settings.s3_secret_access_key:
        options["aws_access_key_id"] = settings.s3_access_key_id
        options["aws_secret_access_key"] = settings.s3_secret_access_key
    if settings.s3_endpoint_url:
        options["config"] = Config(s3={"addressing_style": S3_ADDRESSING_STYLE})
    return boto3.client(S3_STORAGE_BACKEND_NAME, **options)


def normalized_prefix(prefix: str) -> str:
    """`prefix` without leading or trailing slashes; blank falls back to `media`."""
    stripped = (prefix or "").strip().strip("/").strip()
    return stripped or DEFAULT_S3_MEDIA_PREFIX


def s3_object_key(
    *,
    settings: MediaSettings,
    kind: MediaKind,
    content_type: str,
    name: str,
) -> str:
    """`{prefix}/{subdir}/{name}{ext}`: never content-addressed.

    The digest is unknown before the bytes arrive, so the key is reserved with a random
    name instead, and the digest is recorded on the asset row once the object is read.
    """
    return (
        f"{normalized_prefix(settings.s3_media_prefix)}"
        f"/{KIND_SUBDIRS[kind]}/{name}{ALLOWED_CONTENT_TYPES[kind][content_type]}"
    )


def _is_missing_object(error: BaseException) -> bool:
    """Whether this client error means the key is absent rather than storage breaking."""
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    body = response.get("Error")
    code = str(body.get("Code", "")) if isinstance(body, dict) else ""
    metadata = response.get("ResponseMetadata")
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return code in S3_MISSING_OBJECT_CODES or status == 404


class S3StorageBackend:
    """Stores media objects in one S3 bucket, under `S3_MEDIA_PREFIX`.

    The client is injectable, exactly like `EventPublisher(producer=...)`, so every
    operation is testable without a bucket; when none is injected the real client is
    built through `build_s3_client`. Only `health` answers instead of raising: every
    other failure surfaces to the route, which is what turns a broken bucket into an
    error response rather than a silently empty body.
    """

    def __init__(self, settings: MediaSettings, *, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client if client is not None else build_s3_client(settings)

    def public_url(self, key: str) -> str:
        """`/media/{key}`: a private object is served by the app's own proxy route."""
        return f"{MEDIA_URL_PREFIX}/{key}"

    def prepare_key(self, *, kind: MediaKind, content_type: str) -> str:
        """Reserve the key the browser's direct upload will land on."""
        return s3_object_key(
            settings=self.settings,
            kind=kind,
            content_type=content_type,
            name=uuid.uuid4().hex,
        )

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
        """Spool the chunk stream, then put it under a reserved key with its sha256."""
        digest = new_hasher(self.settings.hash_algorithm)
        size = 0
        # Rolls to a real temp file past CHUNK_SIZE, so a large upload is never in memory.
        with tempfile.SpooledTemporaryFile(max_size=CHUNK_SIZE) as spool:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise MediaTooLargeError(max_bytes)
                digest.update(chunk)
                spool.write(chunk)
            if size == 0:
                raise EmptyUploadError()

            sha256 = digest.hexdigest()
            key = s3_object_key(
                settings=self.settings,
                kind=kind,
                content_type=content_type,
                name=uuid.uuid4().hex,
            )
            spool.seek(0)
            self._put(key=key, body=spool)

        return StoredObject(key=key, size=size, sha256=sha256)

    def _put(self, *, key: str, body: Any) -> None:
        """Put one object privately, encrypted when `S3_SSE=AES256` asks for it."""
        options: dict[str, Any] = {"ACL": S3_ACL_PRIVATE}
        if self.settings.s3_sse == S3_SSE_AES256:
            options["ServerSideEncryption"] = S3_SSE_AES256
        self._client.put_object(
            Bucket=self.settings.s3_bucket, Key=key, Body=body, **options
        )

    async def exists(self, *, key: str) -> bool:
        """Whether the bucket holds an object under `key` right now."""
        try:
            self._client.head_object(Bucket=self.settings.s3_bucket, Key=key)
        except Exception as error:  # a missing key is not a broken bucket
            if _is_missing_object(error):
                return False
            raise
        return True

    async def digest(self, *, key: str, algorithm: str) -> DigestResult | None:
        """Hash the object as it streams; None when the bucket holds no such key."""
        digest = new_hasher(algorithm)
        size = 0
        found = False
        async for chunk in self._stream(key=key, offset=0, length=None):
            found = True
            size += len(chunk)
            digest.update(chunk)
        if not found:
            return None
        return DigestResult(sha256=digest.hexdigest(), size=size)

    async def iter_chunks(
        self, *, key: str, offset: int = 0, length: int | None = None
    ) -> AsyncIterator[bytes]:
        """Yield the object one `CHUNK_SIZE` at a time, sliced by `offset`/`length`.

        The slice becomes one `Range` request, so a browser seeking a trailer fetches
        only the bytes it asked for instead of the whole video.
        """
        async for chunk in self._stream(key=key, offset=offset, length=length):
            yield chunk

    async def _stream(
        self, *, key: str, offset: int, length: int | None
    ) -> AsyncIterator[bytes]:
        """The one streamed GET every read goes through; a missing key yields nothing."""
        if length is not None and length <= 0:
            return
        options: dict[str, Any] = {
            "Bucket": self.settings.s3_bucket,
            "Key": key,
        }
        if offset or length is not None:
            end = "" if length is None else str(offset + length - 1)
            options["Range"] = f"bytes={offset}-{end}"

        try:
            response = self._client.get_object(**options)
        except Exception as error:  # a key that is gone reads as an empty object
            if _is_missing_object(error):
                return
            raise

        body = response["Body"]
        remaining = length
        try:
            while True:
                want = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
                if want <= 0:
                    return
                chunk = body.read(want)
                if not chunk:
                    return
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    async def delete(self, *, key: str) -> None:
        """Remove the object under `key`; a key that is already gone is not an error."""
        try:
            self._client.delete_object(Bucket=self.settings.s3_bucket, Key=key)
        except Exception as error:  # deleting a missing object is not a failure
            if _is_missing_object(error):
                return
            raise

    def presigned_upload_url(
        self,
        *,
        asset_id: int,
        key: str,
        content_type: str,
        expires_seconds: int,
    ) -> PresignedUpload:
        """The short-lived PUT url the browser sends the bytes to.

        `asset_id` is deliberately unused here: an S3 object is addressed by its key,
        so the key reserved at presign is what the URL names.
        """
        params: dict[str, Any] = {
            "Bucket": self.settings.s3_bucket,
            "Key": key,
            "ContentType": content_type,
        }
        if self.settings.s3_sse == S3_SSE_AES256:
            params["ServerSideEncryption"] = S3_SSE_AES256
        url = self._client.generate_presigned_url(
            S3_PRESIGNED_UPLOAD_OPERATION,
            Params=params,
            ExpiresIn=expires_seconds,
        )
        return PresignedUpload(
            url=url,
            method="PUT",
            headers={"Content-Type": content_type},
        )

    def presigned_download_url(self, *, key: str, expires_seconds: int) -> str:
        """A short-lived GET url for the object; the app proxy is what clients read."""
        return self._client.generate_presigned_url(
            S3_PRESIGNED_DOWNLOAD_OPERATION,
            Params={"Bucket": self.settings.s3_bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )

    def health(self) -> StorageHealth:
        """Reachable exactly when the bucket answers a `head_bucket`; never raises."""
        bucket = self.settings.s3_bucket or None
        if bucket is None:
            return StorageHealth(
                backend=S3_STORAGE_BACKEND_NAME, bucket=None, reachable=False
            )
        try:
            self._client.head_bucket(Bucket=bucket)
        except Exception as exc:  # a probe must never turn the route into a 5xx
            logger.warning(
                "s3 media bucket health check failed bucket=%s: %s", bucket, exc
            )
            return StorageHealth(
                backend=S3_STORAGE_BACKEND_NAME, bucket=bucket, reachable=False
            )
        return StorageHealth(
            backend=S3_STORAGE_BACKEND_NAME, bucket=bucket, reachable=True
        )
