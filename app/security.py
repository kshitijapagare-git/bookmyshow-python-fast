import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import User

JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-insecure-jwt-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_TTL_SECONDS = 604800

UNAUTHORIZED_DETAIL = "Unauthorized"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


def generate_code() -> str:
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
