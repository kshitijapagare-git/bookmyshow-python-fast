import base64
import binascii

from cryptography.exceptions import InvalidKey
from fastapi import APIRouter, HTTPException, Request, status

from ..crypto.config import PUBLIC_KEY_ALG, SESSION_TTL_SECONDS
from ..crypto.envelope import AES_KEY_BYTES
from ..crypto.keys import public_jwk, unwrap_session_key
from ..crypto.schemas import PublicKeyOut, SessionIn, SessionOut

router = APIRouter(prefix="/api/crypto", tags=["crypto"])

UNWRAP_DETAIL = "Request could not be decrypted"


@router.get("/public-key", response_model=PublicKeyOut)
def public_key(request: Request) -> PublicKeyOut:
    """Publish the server's RSA-OAEP public key so a browser can wrap its session key."""
    pair = request.app.state.crypto_keys
    return PublicKeyOut(kid=pair.kid, alg=PUBLIC_KEY_ALG, jwk=public_jwk(pair))


@router.post("/session", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def open_session(payload: SessionIn, request: Request) -> SessionOut:
    """Unwrap the AES-256 key and keep it in memory for the next 30 minutes."""
    pair = request.app.state.crypto_keys
    try:
        if payload.kid != pair.kid:
            raise ValueError(f"Unknown kid: {payload.kid}")
        session_key = unwrap_session_key(
            pair, base64.b64decode(payload.wrappedKey, validate=True)
        )
    except (binascii.Error, InvalidKey, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=UNWRAP_DETAIL
        ) from None

    if len(session_key) != AES_KEY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=UNWRAP_DETAIL
        )

    session = request.app.state.crypto_sessions.create(session_key)
    return SessionOut(sessionId=session.id, expiresInSeconds=SESSION_TTL_SECONDS)
