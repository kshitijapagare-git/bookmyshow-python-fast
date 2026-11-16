"""The S3 backing for media storage: one backend behind the `StorageBackend` seam.

`S3StorageBackend` implements exactly the members `StorageBackend` declares, so the
upload, download, trailer, integrity and presign routes reach a bucket through the calls
they already make for a local directory. Object keys keep the local backend's relative
convention -- `{subdir}/{hash}{ext}`, or `{subdir}/{assetId}{ext}` for a direct upload
whose bytes have not been hashed yet -- and `S3_MEDIA_PREFIX` is prepended only on the
wire, so `media_assets.storage_key` holds the same kind of value either way.

`boto3` is imported inside `build_s3_client` and never at module import, so the app and
the whole test suite still run on a host without it; a test injects its own client
through `S3StorageBackend(client=...)`. Writes go through `put_object` with a private ACL
-- kept to that one call, because a bucket with Object Ownership enforced rejects the
header -- and, only when `S3_SSE` is configured, server-side encryption. Reads go through
`get_object`, which is also what a presigned GET hands the browser: an object's ACL stays
private and `public_url` regenerates a short-lived signed url on every read instead of
the row storing one that would expire.

A client error is inspected through the `response` dict botocore puts on it rather than
by importing `ClientError`, so a 404 needs no botocore on the host running the tests.
"""

import logging
import tempfile
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from fastapi import UploadFile

from .media import (
    ALLOWED_CONTENT_TYPES,
    CHUNK_SIZE,
    KIND_SUBDIRS,
    ChunkSource,
    EmptyUploadError,
    ExactSizeMismatchError,
    MediaSettings,
    MediaTooLargeError,
    ObjectHead,
    PresignedUpload,
    S3_STORAGE_BACKEND,
    StorageHealth,
    StoredObject,
    UnsupportedMediaTypeError,
    new_hasher,
    object_key,
)
from .schemas import MediaKind
from .streaming import ByteRange

logger = logging.getLogger(__name__)

S3_READ_CHUNK_SIZE = 64 * 1024
S3_SSE_HEADER = "x-amz-server-side-encryption"
S3_PRIVATE_ACL = "private"
NOT_FOUND_CODES: frozenset[str] = frozenset({"404", "NoSuchKey", "NotFound"})


@lru_cache(maxsize=16)
def build_s3_client(settings: MediaSettings) -> Any:
    """The bucket client for these settings; boto3 is imported here, never at module load.

    `endpoint_url` is passed only when one is configured, so the same code talks to AWS
    and to a LocalStack or MinIO endpoint, and the credentials are passed only when they
    are configured, leaving the instance's own credentials to answer otherwise. The
    client is memoised on the (frozen, hashable) settings, because the backend itself is
    built per request: every request for one configuration shares one connection pool.
    """
    import boto3

    options: dict[str, Any] = {"region_name": settings.s3_region}
    if settings.s3_endpoint_url:
        options["endpoint_url"] = settings.s3_endpoint_url
    if settings.s3_access_key_id:
        options["aws_access_key_id"] = settings.s3_access_key_id
    if settings.s3_secret_access_key:
        options["aws_secret_access_key"] = settings.s3_secret_access_key
    return boto3.client("s3", **options)


def _is_not_found(error: BaseException) -> bool:
    """Whether the client's error says the object is simply not there."""
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    details = response.get("Error")
    code = details.get("Code") if isinstance(details, dict) else None
    if code is not None and str(code) in NOT_FOUND_CODES:
        return True
    metadata = response.get("ResponseMetadata")
    status_code = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return status_code == 404


class S3StorageBackend:
    """Stores media objects in one S3 bucket, under the configured prefix."""

    def __init__(self, settings: MediaSettings, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def client(self) -> Any:
        """The injected client, or the real one built from the S3_* settings on first use."""
        if self._client is None:
            self._client = build_s3_client(self.settings)
        return self._client

    def _wire_key(self, key: str) -> str:
        """The bucket key for a backend-relative key: the prefix plus a single '/'."""
        prefix = self.settings.s3_media_prefix
        return f"{prefix}/{key}" if prefix else key

    def _put(self, *, key: str, content_type: str, body: Any) -> None:
        """Write one object privately, adding the SSE parameter only when configured."""
        params: dict[str, Any] = {
            "Bucket": self.settings.s3_bucket,
            "Key": self._wire_key(key),
            "Body": body,
            "ContentType": content_type,
            "ACL": S3_PRIVATE_ACL,
        }
        if self.settings.s3_sse is not None:
            params["ServerSideEncryption"] = self.settings.s3_sse
        self.client.put_object(**params)

    async def save(
        self,
        *,
        kind: MediaKind,
        content_type: str,
        upload: UploadFile,
        max_bytes: int,
    ) -> StoredObject:
        """Spool the stream while hashing it, then put it under its sha-derived key.

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
        """Spool the body, and put it only when it is exactly the declared size."""
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
        params: dict[str, Any] = {
            "Bucket": self.settings.s3_bucket,
            "Key": self._wire_key(key),
        }
        if byte_range is not None:
            params["Range"] = f"bytes={byte_range.start}-{byte_range.end}"
        response = self.client.get_object(**params)
        body = response["Body"]
        try:
            yield from body.iter_chunks(S3_READ_CHUNK_SIZE)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def read_bytes(self, key: str) -> bytes:
        """The whole object; only for objects already known to be small."""
        response = self.client.get_object(
            Bucket=self.settings.s3_bucket, Key=self._wire_key(key)
        )
        return response["Body"].read()

    def delete(self, key: str) -> None:
        """Delete the object; one that is already gone is not an error."""
        try:
            self.client.delete_object(
                Bucket=self.settings.s3_bucket, Key=self._wire_key(key)
            )
        except Exception as error:
            if _is_not_found(error):
                return
            raise

    def head(self, key: str) -> ObjectHead | None:
        """The object's size and content type without reading it; None when absent."""
        try:
            response = self.client.head_object(
                Bucket=self.settings.s3_bucket, Key=self._wire_key(key)
            )
        except Exception as error:
            if _is_not_found(error):
                return None
            raise
        return ObjectHead(
            size=int(response.get("ContentLength") or 0),
            content_type=response.get("ContentType"),
        )

    def exists(self, key: str) -> bool:
        """Whether an object is stored under this key right now."""
        return self.head(key) is not None

    def public_url(self, key: str) -> str:
        """A freshly signed GET url, because the objects themselves stay private."""
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.settings.s3_bucket,
                "Key": self._wire_key(key),
            },
            ExpiresIn=self.settings.presign_expires_seconds,
        )

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
        digest is not known before the bytes arrive; `save`/`save_exact` keep the
        content-addressed key for the uploads that pass through this API.
        """
        extension = ALLOWED_CONTENT_TYPES.get(kind, {}).get(content_type)
        if extension is None:
            raise UnsupportedMediaTypeError(content_type)

        key = f"{KIND_SUBDIRS[kind]}/{asset_id}{extension}"
        params: dict[str, Any] = {
            "Bucket": self.settings.s3_bucket,
            "Key": self._wire_key(key),
            "ContentType": content_type,
        }
        headers = {"Content-Type": content_type}
        if self.settings.s3_sse is not None:
            params["ServerSideEncryption"] = self.settings.s3_sse
            headers[S3_SSE_HEADER] = self.settings.s3_sse

        return PresignedUpload(
            url=self.client.generate_presigned_url(
                "put_object",
                Params=params,
                ExpiresIn=self.settings.presign_expires_seconds,
            ),
            method="PUT",
            headers=headers,
            key=key,
        )

    def health(self) -> StorageHealth:
        """Whether the bucket answers right now; never raises, it reports instead."""
        if not self.settings.s3_bucket:
            return StorageHealth(backend=S3_STORAGE_BACKEND, reachable=False)
        try:
            self.client.head_bucket(Bucket=self.settings.s3_bucket)
        except Exception as error:  # a probe must report down, never fail the route
            logger.warning(
                "s3 media bucket %r is unreachable: %s", self.settings.s3_bucket, error
            )
            return StorageHealth(backend=S3_STORAGE_BACKEND, reachable=False)
        return StorageHealth(backend=S3_STORAGE_BACKEND, reachable=True)
