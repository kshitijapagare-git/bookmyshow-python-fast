"""HMAC-signed tokens that authorise one raw-bytes upload to the local backend.

The browser PUTs a presigned file straight to `PUT /api/media/{assetId}/upload` with
the raw bytes and neither a JWT nor an encryption session, so the query-string token
is that route's only authorisation. It binds one asset id to one expiry epoch second:
`{expires_at}.{base64url HMAC-SHA256(secret, "{asset_id}.{expires_at}")}`.

Verification recomputes the signature for the asset id the URL names and compares it
with `secrets.compare_digest`, so a tampered signature, a token minted for a different
asset and an expired token all raise `PresignTokenError` -- which the route answers as
the ticket's 403. `now` is injectable so a test can produce an expired token without
sleeping.
"""

import base64
import hashlib
import hmac
import secrets
import time

TOKEN_SEPARATOR = "."


class PresignTokenError(Exception):
    """The upload token is malformed, tampered, names another asset or has expired."""


def _signature(*, asset_id: int, expires_at: int, secret: str) -> bytes:
    """The HMAC-SHA256 over `{asset_id}.{expires_at}` keyed by the presign secret."""
    message = f"{asset_id}{TOKEN_SEPARATOR}{expires_at}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64url(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded)


def sign_presign_token(*, asset_id: int, expires_at: int, secret: str) -> str:
    """Sign one asset id and expiry: `{expires_at}.{base64url signature}`."""
    signature = _signature(asset_id=asset_id, expires_at=expires_at, secret=secret)
    return f"{expires_at}{TOKEN_SEPARATOR}{_b64url(signature)}"


def verify_presign_token(
    *, token: str, asset_id: int, secret: str, now: int | None = None
) -> int:
    """The token's expiry epoch second, or PresignTokenError when it is not valid now."""
    expires_text, separator, encoded = (token or "").partition(TOKEN_SEPARATOR)
    if not separator or not expires_text or not encoded:
        raise PresignTokenError("upload token is malformed")
    try:
        expires_at = int(expires_text)
    except ValueError:
        raise PresignTokenError("upload token is malformed") from None
    try:
        presented = _unb64url(encoded)
    except ValueError:
        raise PresignTokenError("upload token is malformed") from None

    expected = _signature(asset_id=asset_id, expires_at=expires_at, secret=secret)
    if not secrets.compare_digest(presented, expected):
        raise PresignTokenError("upload token does not match this asset")

    current = int(time.time()) if now is None else now
    if current >= expires_at:
        raise PresignTokenError("upload token has expired")
    return expires_at
