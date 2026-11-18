"""Where the generated API documentation is served, and whether it is served at all.

The settings follow the same shape as the repo's other per-feature settings:
`MediaSettings` and `EventBusSettings` are frozen dataclasses with an env-reading
`from_env()` that falls back rather than raising, and `CryptoSettings` turns a knob off
only when its stripped lowercase value is `false`. `DocsSettings` does both, so a blank
`DOCS_URL` keeps the default path instead of registering an empty one.

The three paths are normalised here rather than by the caller: `create_app` hands them
to the `FastAPI` constructor and the crypto middleware puts them on its public list, so
a path handed to FastAPI must be byte-identical to the one the encryption layer exempts.
`bypass_paths()` therefore returns the three configured paths unconditionally -- a
deployment with `DOCS_ENABLED=false` still exempts them, so its 404 comes back as a
plain ProblemDetail rather than as the middleware's 428.
"""

import os
from dataclasses import dataclass

DEFAULT_DOCS_ENABLED = True
DEFAULT_DOCS_URL = "/docs"
DEFAULT_OPENAPI_URL = "/openapi.json"
DEFAULT_REDOC_URL = "/redoc"


def _env(name: str, default: str) -> str:
    """The environment value, or the default when it is absent or blank."""
    return (os.getenv(name) or "").strip() or default


def _flag(name: str, default: bool) -> bool:
    """A boolean that is off only when its value reads as `false`.

    This is the convention `CryptoSettings` already uses for `CRYPTO_ENABLED`: an
    absent, blank or unrecognised value keeps documentation enabled.
    """
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw != "false"


def _path(name: str, default: str) -> str:
    """A route path with one leading '/' and no trailing one, so both sides match.

    `create_app` registers the path and the crypto middleware exempts it, so the two
    strings have to be identical; a bare '/' is left alone rather than emptied.
    """
    raw = _env(name, default).strip()
    normalised = f"/{raw.lstrip('/')}" if raw else default
    if normalised == "/":
        return normalised
    return normalised.rstrip("/") or "/"


@dataclass(frozen=True)
class DocsSettings:
    """Which paths serve Swagger UI, ReDoc and the OpenAPI document, if any."""

    enabled: bool = DEFAULT_DOCS_ENABLED
    docs_url: str = DEFAULT_DOCS_URL
    openapi_url: str = DEFAULT_OPENAPI_URL
    redoc_url: str = DEFAULT_REDOC_URL

    @classmethod
    def from_env(cls) -> "DocsSettings":
        """The configured settings; a blank or unparseable value falls back, never raises."""
        return cls(
            enabled=_flag("DOCS_ENABLED", DEFAULT_DOCS_ENABLED),
            docs_url=_path("DOCS_URL", DEFAULT_DOCS_URL),
            openapi_url=_path("OPENAPI_URL", DEFAULT_OPENAPI_URL),
            redoc_url=_path("REDOC_URL", DEFAULT_REDOC_URL),
        )

    def bypass_paths(self) -> frozenset[str]:
        """The three configured paths, exempt from the encryption layer either way.

        Returned independently of `enabled`: a deployment with documentation switched
        off still answers these paths, and must answer them plainly.
        """
        return frozenset({self.docs_url, self.openapi_url, self.redoc_url})
