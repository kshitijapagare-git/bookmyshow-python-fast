import base64
import binascii
import hashlib
import hmac
import json
import os

from fastapi import Header, HTTPException, status

JWT_SECRET = os.getenv("JWT_SECRET", "dev-jwt-secret")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
    )


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def get_current_user_id(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> int:
    if authorization is None or not authorization.startswith("Bearer "):
        raise _unauthorized()

    token = authorization[len("Bearer ") :].strip()
    parts = token.split(".")
    if len(parts) != 3:
        raise _unauthorized()

    signing_input = f"{parts[0]}.{parts[1]}".encode()
    expected = hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    try:
        provided = _b64decode(parts[2])
    except (binascii.Error, ValueError):
        raise _unauthorized() from None

    if not hmac.compare_digest(expected, provided):
        raise _unauthorized()

    try:
        payload = json.loads(_b64decode(parts[1]))
        return int(payload["sub"])
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise _unauthorized() from None
