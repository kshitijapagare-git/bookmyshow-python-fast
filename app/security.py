import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import User

logger = logging.getLogger(__name__)

JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-insecure-jwt-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_TTL_SECONDS = 604800

OTP_DEV_FIXED_CODE = os.getenv("OTP_DEV_FIXED_CODE", "").strip()
if OTP_DEV_FIXED_CODE and not re.fullmatch(r"\d{6}", OTP_DEV_FIXED_CODE):
    raise RuntimeError("OTP_DEV_FIXED_CODE must be exactly 6 digits")
if OTP_DEV_FIXED_CODE:
    logger.warning("OTP_DEV_FIXED_CODE is set: every OTP uses the fixed development code")

UNAUTHORIZED_DETAIL = "Unauthorized"
BOOKINGS_SERVICE_TOKEN = os.getenv(
    "BOOKINGS_SERVICE_TOKEN", "dev-service-token"
).strip()
INVALID_SERVICE_TOKEN_DETAIL = "Invalid service token"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


def generate_code() -> str:
    if OTP_DEV_FIXED_CODE:
        return OTP_DEV_FIXED_CODE
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(*, code: str) -> str:
    return bcrypt.hashpw(code.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_code(*, code: str, code_hash: str) -> bool:
    try:
        return bcrypt.checkpw(code.encode("utf-8"), code_hash.encode("utf-8"))
    except ValueError:
        return False


def issue_token(*, user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(seconds=JWT_TTL_SECONDS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(*, token: str) -> int:
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    return int(payload["sub"])


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if authorization is None or not authorization.strip():
        raise _unauthorized()

    scheme, _, raw_token = authorization.partition(" ")
    token = raw_token.strip()
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized()

    try:
        user_id = decode_token(token=token)
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise _unauthorized() from None

    user = db.get(User, user_id)
    if user is None:
        raise _unauthorized()
    return user


def get_optional_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User | None:
    """Resolve the bearer token when it is valid, otherwise treat the caller as a guest."""
    if authorization is None or not authorization.strip():
        return None

    scheme, _, raw_token = authorization.partition(" ")
    token = raw_token.strip()
    if scheme.lower() != "bearer" or not token:
        return None

    try:
        user_id = decode_token(token=token)
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        return None

    return db.get(User, user_id)


def require_service_token(
    x_service_token: Annotated[str | None, Header(alias="X-Service-Token")] = None,
) -> None:
    """The shared token the other services present; a missing or wrong one is a 401."""
    presented = (x_service_token or "").strip()
    if not presented or not secrets.compare_digest(presented, BOOKINGS_SERVICE_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_SERVICE_TOKEN_DETAIL,
        )


MEDIA_SERVICE_TOKEN = os.getenv("MEDIA_SERVICE_TOKEN", "dev-service-token").strip()
INVALID_MEDIA_TOKEN_DETAIL = "Invalid media service token"


def require_media_service_token(
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> None:
    """The catalogue operator's token for media uploads; missing or wrong is a 401."""
    presented = (x_internal_token or "").strip()
    if not presented or not secrets.compare_digest(presented, MEDIA_SERVICE_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_MEDIA_TOKEN_DETAIL,
        )
