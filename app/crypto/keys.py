import base64
import logging
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .config import KID, CryptoSettings

logger = logging.getLogger(__name__)

RSA_KEY_BITS = 3072


@dataclass(frozen=True)
class ServerKeyPair:
    kid: str
    private_key: rsa.RSAPrivateKey
    public_key: rsa.RSAPublicKey


def oaep_padding() -> padding.OAEP:
    """OAEP with SHA-256 for the digest *and* MGF1, matching Web Crypto's RSA-OAEP-256."""
    return padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(),
        label=None,
    )


def load_key_pair(settings: CryptoSettings) -> ServerKeyPair:
    if settings.private_key_pem:
        private_key = serialization.load_pem_private_key(
            settings.private_key_pem.encode("utf-8"), password=None
        )
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError(
                "CRYPTO_PRIVATE_KEY must be an RSA private key in PKCS#8 PEM form"
            )
    else:
        private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=RSA_KEY_BITS
        )
        logger.warning(
            "CRYPTO_PRIVATE_KEY is not set; generated a temporary RSA key pair for this process"
        )
    return ServerKeyPair(
        kid=KID,
        private_key=private_key,
        public_key=private_key.public_key(),
    )


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def public_jwk(pair: ServerKeyPair) -> dict[str, str]:
    numbers = pair.public_key.public_numbers()
    return {
        "kty": "RSA",
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }


def unwrap_session_key(pair: ServerKeyPair, wrapped_key: bytes) -> bytes:
    return pair.private_key.decrypt(wrapped_key, oaep_padding())
