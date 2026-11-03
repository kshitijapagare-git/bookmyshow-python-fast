import os
from dataclasses import dataclass

KID = "2026-10-a"
PUBLIC_KEY_ALG = "RSA-OAEP-256"
ENCRYPTED_CONTENT_TYPE = "application/vnd.bookmyshow.enc+json"
SESSION_TTL_SECONDS = 1800
CLOCK_SKEW_SECONDS = 60
ENVELOPE_VERSION = 1
IV_BYTES = 12
TAG_BITS = 128


@dataclass(frozen=True)
class CryptoSettings:
    enabled: bool
    private_key_pem: str | None

    @classmethod
    def from_env(cls) -> "CryptoSettings":
        raw_enabled = os.getenv("CRYPTO_ENABLED")
        enabled = True if raw_enabled is None else raw_enabled.strip().lower() != "false"
        return cls(
            enabled=enabled,
            private_key_pem=os.getenv("CRYPTO_PRIVATE_KEY") or None,
        )
