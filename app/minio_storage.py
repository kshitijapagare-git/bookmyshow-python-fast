"""The MinIO backing for media storage: the S3 backend pointed at a MinIO endpoint.

MinIO speaks the S3 API, so the whole storage seam is already implemented by
`S3StorageBackend`: `MinioStorageBackend` subclasses it and changes only two things.
Object keys keep the same relative convention -- `{subdir}/{hash}{ext}`, or
`{subdir}/{assetId}{ext}` for a direct upload whose bytes have not been hashed yet --
and `MINIO_MEDIA_PREFIX` goes on the wire exactly as `S3_MEDIA_PREFIX` does, so
`media_assets.storage_key` holds the same kind of value on every backend.

First, the `MINIO_*` knobs are mapped onto a derived `MediaSettings`
(`minio_settings`): the bucket becomes `s3_bucket`, the prefix `s3_media_prefix`, the
endpoint `s3_endpoint_url` (with a scheme, see `minio_endpoint_url`) and the access and
secret keys the S3 credentials, so every inherited method addresses the MinIO bucket
without the base class knowing MinIO exists. That is why `MinioStorageBackend.settings`
is not the object the caller handed in.

Second, the client is built through `build_minio_client`, memoised on those settings
just as `build_s3_client` is: `boto3` and `botocore.config` are imported inside the
call and never at module import, so the app and the test suite still run on a host
without boto3, and a test injects its own client through
`MinioStorageBackend(client=...)`. Path-style addressing is what makes both the object
requests and the presigned urls resolve to the MinIO endpoint itself instead of a
`{bucket}.` subdomain of it.

Bucket creation is opt-in (`MINIO_CREATE_BUCKET`) and lazy: the first operation on an
instance heads the bucket and creates it only when MinIO reports it missing, so a dev
container needs no manual `mc mb`. A head that fails for any other reason, and a
creation that fails, surface as the operation's own error -- an operation never
pretends the bucket is there.
"""

import logging
from dataclasses import replace
from functools import lru_cache
from typing import Any

from .media import MINIO_STORAGE_BACKEND, MediaSettings, StorageHealth
from .s3_storage import S3StorageBackend, _is_not_found

logger = logging.getLogger(__name__)

MINIO_ADDRESSING_STYLE = "path"


def minio_endpoint_url(settings: MediaSettings) -> str:
    """The MinIO endpoint as a url: a bare `host:port` gains the scheme `MINIO_SECURE` names.

    An endpoint that already carries `http://` or `https://` is kept as configured, so a
    deployment behind a proxy or a TLS terminator can say exactly what it means, and a
    blank endpoint stays blank: the caller is the one that decides what a missing
    endpoint means.
    """
    endpoint = (settings.minio_endpoint or "").strip()
    if not endpoint:
        return ""
    if endpoint.lower().startswith(("http://", "https://")):
        return endpoint
    scheme = "https" if settings.minio_secure else "http"
    return f"{scheme}://{endpoint}"


def minio_settings(settings: MediaSettings) -> MediaSettings:
    """The app's settings with the `MINIO_*` knobs mapped onto the inherited S3 ones.

    `S3StorageBackend` reads `s3_bucket`, `s3_media_prefix`, `s3_endpoint_url` and the
    two credential fields for the bucket key, the wire key and the client, so mapping
    MinIO onto them is what lets the whole seam be inherited unchanged. The remaining
    MinIO fields -- `minio_create_bucket` above all -- travel through untouched.
    """
    return replace(
        settings,
        s3_bucket=settings.minio_bucket,
        s3_media_prefix=settings.minio_media_prefix,
        s3_endpoint_url=minio_endpoint_url(settings),
        s3_access_key_id=settings.minio_access_key,
        s3_secret_access_key=settings.minio_secret_key,
    )


@lru_cache(maxsize=16)
def build_minio_client(settings: MediaSettings) -> Any:
    """The MinIO client for these (already derived) settings; boto3 is imported here.

    Path-style addressing is passed in a botocore `Config`, because MinIO addresses a
    bucket as a path on one endpoint rather than as a subdomain; without it boto3 would
    sign and send requests for `{bucket}.{endpoint}` that MinIO does not serve. The
    credentials are passed only when they are configured, leaving the instance's own
    environment to answer otherwise, and a blank endpoint raises rather than letting
    boto3 fall back to its default -- which is AWS S3, the one place these settings must
    never send a MinIO deployment's bytes. The client is memoised on the frozen,
    hashable settings because the backend itself is built per request.
    """
    endpoint = settings.s3_endpoint_url or minio_endpoint_url(settings)
    if not endpoint:
        raise ValueError(
            "MINIO_ENDPOINT is not set, so there is no MinIO endpoint to address"
        )

    import boto3
    from botocore.config import Config

    options: dict[str, Any] = {
        "region_name": settings.s3_region,
        "endpoint_url": endpoint,
        "config": Config(s3={"addressing_style": MINIO_ADDRESSING_STYLE}),
    }
    if settings.s3_access_key_id:
        options["aws_access_key_id"] = settings.s3_access_key_id
    if settings.s3_secret_access_key:
        options["aws_secret_access_key"] = settings.s3_secret_access_key
    return boto3.client("s3", **options)


class MinioStorageBackend(S3StorageBackend):
    """Stores media objects in one MinIO bucket, through the S3-compatible API.

    Everything the seam declares -- put, get, stream, delete, exists, head, the
    presigned urls and the save paths -- is the S3 backend's implementation, run against
    the derived settings and the MinIO client. Only the client, the lazy bucket creation
    and the health report are MinIO's own.
    """

    def __init__(self, settings: MediaSettings, client: Any | None = None) -> None:
        super().__init__(minio_settings(settings), client=client)
        self._bucket_ready = False

    @property
    def client(self) -> Any:
        """The injected client, or the real MinIO one, with the bucket ensured once."""
        if self._client is None:
            self._client = build_minio_client(self.settings)
        self._ensure_bucket(self._client)
        return self._client

    def _ensure_bucket(self, client: Any) -> None:
        """Create the bucket once when `MINIO_CREATE_BUCKET` asks for it, then remember it.

        A head that reports the bucket missing is the only case that creates anything;
        any other failure -- a denied head, or a creation that fails -- is re-raised, so
        it surfaces as the error of the operation that triggered it instead of a put
        against a bucket that is not there. Nothing is probed at all when creation is
        off, and the probe happens once per instance rather than once per call.
        """
        if not self.settings.minio_create_bucket or self._bucket_ready:
            return
        try:
            client.head_bucket(Bucket=self.settings.s3_bucket)
        except Exception as error:
            if not _is_not_found(error):
                raise
            client.create_bucket(Bucket=self.settings.s3_bucket)
        self._bucket_ready = True

    def health(self) -> StorageHealth:
        """Whether the MinIO bucket answers right now, plus the endpoint it answers on.

        A probe must report down rather than fail the route, so nothing here raises: an
        unreachable endpoint, a denied head and a client that cannot even be built all
        read as `reachable=False`. With `MINIO_CREATE_BUCKET` on, this probe is a bucket
        operation like any other, so the first one may create the bucket.
        """
        if not self.settings.s3_bucket:
            return StorageHealth(
                backend=MINIO_STORAGE_BACKEND,
                reachable=False,
                bucket=self.settings.s3_bucket,
                endpoint=self.settings.s3_endpoint_url,
            )
        try:
            self.client.head_bucket(Bucket=self.settings.s3_bucket)
        except Exception as error:  # a probe must report down, never fail the route
            logger.warning(
                "minio media bucket %r at %r is unreachable: %s",
                self.settings.s3_bucket,
                self.settings.s3_endpoint_url,
                error,
            )
            return StorageHealth(
                backend=MINIO_STORAGE_BACKEND,
                reachable=False,
                bucket=self.settings.s3_bucket,
                endpoint=self.settings.s3_endpoint_url,
            )
        return StorageHealth(
            backend=MINIO_STORAGE_BACKEND,
            reachable=True,
            bucket=self.settings.s3_bucket,
            endpoint=self.settings.s3_endpoint_url,
        )
