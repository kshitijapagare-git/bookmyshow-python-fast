import base64
import secrets
from typing import Literal

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel

from .config import ENVELOPE_VERSION, IV_BYTES, TAG_BITS

AES_KEY_BYTES = 32


class Envelope(BaseModel):
    """The `{v, iv, ct}` wire format every encrypted body uses."""

    v: Literal[1]
    iv: str
    ct: str


def request_aad(
    *, method: str, path_with_query: str, timestamp: str, session_id: str
) -> bytes:
    """Bind a request body to its method, URL, timestamp and session."""
    return "\n".join((method, path_with_query, timestamp, session_id)).encode("utf-8")


def response_aad(*, status_code: int, request_iv: str) -> bytes:
    """Bind a response body to the status and the IV of the request it answers."""
    return f"{status_code}\n{request_iv}".encode("utf-8")


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(raw: str) -> bytes:
    return base64.b64decode(raw, validate=True)


def _session_key(key: bytes) -> bytes:
    if len(key) != AES_KEY_BYTES:
        raise ValueError("AES-256-GCM session key must be 32 bytes")
    return key


def seal(*, key: bytes, plaintext: bytes, aad: bytes) -> Envelope:
    """Encrypt with a fresh 12-byte IV and a 128-bit GCM tag."""
    iv = secrets.token_bytes(IV_BYTES)
    ciphertext = AESGCM(_session_key(key)).encrypt(iv, plaintext, aad)
    return Envelope(
        v=ENVELOPE_VERSION,
        iv=_b64encode(iv),
        ct=_b64encode(ciphertext),
    )


def open_envelope(*, key: bytes, envelope: Envelope, aad: bytes) -> bytes:
    """Decrypt an envelope, raising InvalidTag/ValueError for anything altered."""
    if envelope.v != ENVELOPE_VERSION:
        raise ValueError(f"Unsupported envelope version: {envelope.v}")
    iv = _b64decode(envelope.iv)
    if len(iv) != IV_BYTES:
        raise ValueError(f"Envelope iv must be {IV_BYTES} bytes")
    ciphertext = _b64decode(envelope.ct)
    if len(ciphertext) < TAG_BITS // 8:
        raise ValueError("Envelope ciphertext is too short to carry a GCM tag")
    return AESGCM(_session_key(key)).decrypt(iv, ciphertext, aad)
