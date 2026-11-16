"""Signed upload tokens: the authorization a direct-to-storage upload carries.

The local backend's presigned upload url is a bare `PUT` with no encryption session
and no bearer token, so the token in its query string is the whole authorization. It
is a PyJWT grant -- the same library `app.security` already signs with -- using HS256
over `MEDIA_PRESIGN_SECRET`, carrying the asset it was minted for, the kind the bytes
will be stored under, and the size and content type the client declared at presign.
The upload route compares all four with what it was handed, so an altered or
foreign-signed token, an expired one, or one naming an unknown kind is refused with
`PresignTokenError` rather than parsed into a grant.

Nothing in this module imports `app.routers`, so the media router can depend on it
without a cycle.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from .media import MediaSettings
from .schemas import MediaKind

PRESIGN_ALGORITHM = "HS256"
ASSET_ID_CLAIM = "assetId"
KIND_CLAIM = "kind"
SIZE_CLAIM = "size"
CONTENT_TYPE_CLAIM = "contentType"
UPLOAD_URL_TEMPLATE = "/api/media/{asset_id}/upload?token={token}"


@dataclass(frozen=True)
class UploadGrant:
    """What one token authorises: which asset, which kind and which exact bytes."""

    asset_id: int
    kind: MediaKind
    size: int
    content_type: str


class PresignTokenError(Exception):
    """A missing, malformed, foreign-signed or expired upload token."""


def sign_upload_token(
    settings: MediaSettings,
    *,
    asset_id: int,
    kind: MediaKind,
    size: int,
    content_type: str,
    expires_in_seconds: int | None = None,
) -> str:
    """Mint the grant the upload url carries; the default lifetime is the setting's.

    `expires_in_seconds` exists so a caller -- a test, mostly -- can mint an already
    expired token; the upload route only ever uses the configured lifetime.
    """
    lifetime = (
        settings.presign_expires_seconds
        if expires_in_seconds is None
        else expires_in_seconds
    )
    payload = {
        ASSET_ID_CLAIM: asset_id,
        KIND_CLAIM: kind.value,
        SIZE_CLAIM: size,
        CONTENT_TYPE_CLAIM: content_type,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=lifetime),
    }
    return jwt.encode(payload, settings.presign_secret, algorithm=PRESIGN_ALGORITHM)


def decode_upload_token(settings: MediaSettings, *, token: str) -> UploadGrant:
    """Verify a token and answer what it grants; anything unusable raises."""
    try:
        payload = jwt.decode(
            token, settings.presign_secret, algorithms=[PRESIGN_ALGORITHM]
        )
    except jwt.InvalidTokenError:
        raise PresignTokenError("upload token is not valid") from None

    asset_id = payload.get(ASSET_ID_CLAIM)
    if isinstance(asset_id, bool) or not isinstance(asset_id, int):
        raise PresignTokenError("upload token names no asset")

    size = payload.get(SIZE_CLAIM)
    if isinstance(size, bool) or not isinstance(size, int):
        raise PresignTokenError("upload token carries no size")

    content_type = payload.get(CONTENT_TYPE_CLAIM)
    if not isinstance(content_type, str) or not content_type.strip():
        raise PresignTokenError("upload token carries no content type")

    try:
        kind = MediaKind(payload.get(KIND_CLAIM))
    except ValueError:
        raise PresignTokenError("upload token names an unknown kind") from None

    return UploadGrant(
        asset_id=asset_id,
        kind=kind,
        size=size,
        content_type=content_type,
    )


def upload_url(
    settings: MediaSettings,
    *,
    asset_id: int,
    kind: MediaKind,
    size: int,
    content_type: str,
) -> str:
    """The signed `PUT` url the client uploads the bytes to."""
    token = sign_upload_token(
        settings,
        asset_id=asset_id,
        kind=kind,
        size=size,
        content_type=content_type,
    )
    return UPLOAD_URL_TEMPLATE.format(asset_id=asset_id, token=token)
