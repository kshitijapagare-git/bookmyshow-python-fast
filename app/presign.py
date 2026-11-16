"""The signed capability token a direct-to-storage upload carries.

Presigning hands the browser a short-lived `PUT` url instead of streaming the image
through the API, so the url itself has to prove which asset it may write and until
when. The token is an HMAC-SHA256 over `{asset_id}:{expires_at}` with
`MEDIA_PRESIGN_SECRET` and renders as `{asset_id}:{expires_at}:{hexsignature}`. The
asset id is signed inside the message, so a token minted for one asset is refused for
another even though the id also appears in the path.

Nothing here touches a database or a request, which is what makes the token
unit-testable: `sign_upload_token` and `verify_upload_token` are pure functions, and
`verify_upload_token` answers one boolean for every failure mode -- malformed, other
asset, bad signature, expired -- so the route can answer 403 without revealing which
check failed.
"""

import hashlib
import hmac
import secrets
import time

UPLOAD_METHOD = "PUT"
TOKEN_PARAM = "token"
TOKEN_SEPARATOR = ":"
TOKEN_PARTS = 3

UPLOAD_PATH_TEMPLATE = "/api/media/{asset_id}/upload"


def _signature(*, asset_id: int, expires_at: int, secret: str) -> str:
    """`hex(HMAC-SHA256(secret, '{asset_id}:{expires_at}'))`."""
    message = f"{asset_id}{TOKEN_SEPARATOR}{expires_at}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def sign_upload_token(*, asset_id: int, expires_at: int, secret: str) -> str:
    """The token the upload url carries: the asset id, its expiry and the signature."""
    signature = _signature(asset_id=asset_id, expires_at=expires_at, secret=secret)
    return f"{asset_id}{TOKEN_SEPARATOR}{expires_at}{TOKEN_SEPARATOR}{signature}"


def verify_upload_token(
    *, token: str, asset_id: int, secret: str, now: int | None = None
) -> bool:
    """True only for this asset's own, unexpired, correctly signed token."""
    parts = token.split(TOKEN_SEPARATOR) if token else []
    if len(parts) != TOKEN_PARTS:
        return False

    asset_text, expires_text, signature = parts
    if not asset_text.isdigit() or not expires_text.isdigit():
        return False

    token_asset_id = int(asset_text)
    expires_at = int(expires_text)
    if token_asset_id != asset_id:
        return False

    expected = _signature(
        asset_id=token_asset_id, expires_at=expires_at, secret=secret
    )
    if not secrets.compare_digest(expected, signature):
        return False

    current = int(time.time()) if now is None else now
    return current <= expires_at


def upload_url(*, asset_id: int, token: str) -> str:
    """The local signed `PUT` path the client sends its bytes to."""
    return f"{UPLOAD_PATH_TEMPLATE.format(asset_id=asset_id)}?{TOKEN_PARAM}={token}"
