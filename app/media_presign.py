"""Signed, short-lived tokens for the local backend's direct upload URL.

A presigned upload must not need the moviegoer's JWT, so the URL carries a token
instead: `<expires_at>.<hmac hex>` where the HMAC-SHA256 is taken over
`"{asset_id}:{expires_at}"` with `MEDIA_PRESIGN_SECRET`. The secret never leaves the
server, and because the asset id is inside the signed message a token issued for one
asset is useless for another.

`verify_upload_token` answers the token's expiry for a valid one and refuses the rest
as one of two errors: `PresignTokenExpired` when the expiry is not after now, and
`PresignTokenError` for a malformed token, a signature made with another secret or a
signature made for another asset. The signature is compared with
`secrets.compare_digest`, so a wrong token cannot be discovered byte by byte.
"""

import hashlib
import hmac
import secrets
import time

TOKEN_SEPARATOR = "."


class PresignTokenError(Exception):
    """The token is malformed, signed for another asset or signed with another secret."""


class PresignTokenExpired(PresignTokenError):
    """The token's expiry is not in the future any more."""


def _signature(*, asset_id: int, expires_at: int, secret: str) -> str:
    """The hex HMAC-SHA256 over `{asset_id}:{expires_at}`, keyed by the secret."""
    message = f"{asset_id}:{expires_at}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def sign_upload_token(*, asset_id: int, expires_at: int, secret: str) -> str:
    """`<expires_at>.<hmac hex>`: the whole token is built here, never by callers."""
    signature = _signature(asset_id=asset_id, expires_at=expires_at, secret=secret)
    return f"{expires_at}{TOKEN_SEPARATOR}{signature}"


def verify_upload_token(
    *, token: str, asset_id: int, secret: str, now: float | None = None
) -> int:
    """The token's expiry for a valid token; an exception for anything else."""
    expires_text, separator, signature = (token or "").partition(TOKEN_SEPARATOR)
    if not separator or not expires_text or not signature:
        raise PresignTokenError("malformed upload token")
    try:
        expires_at = int(expires_text)
    except ValueError:
        raise PresignTokenError("malformed upload token") from None

    expected = _signature(asset_id=asset_id, expires_at=expires_at, secret=secret)
    if not secrets.compare_digest(signature, expected):
        raise PresignTokenError("upload token signature does not match")

    current = time.time() if now is None else now
    if expires_at <= current:
        raise PresignTokenExpired("upload token has expired")
    return expires_at
