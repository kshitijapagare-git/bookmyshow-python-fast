"""The served API documentation: which paths answer, and that crypto never touches them.

The three docs routes are the framework's own -- FastAPI registers Swagger UI, ReDoc and
the OpenAPI document from the paths `create_app` hands it -- so these tests pin the wiring
rather than a hand-written page: with the encryption layer switched on and no
`X-Enc-Session` header, all three read plaintext, which they only can because
`create_app` put exactly those configured paths on the middleware's public list.
Switching documentation off registers no such routes at all, so each path falls through to
the app's own ProblemDetail 404 -- a plain body, never the middleware's 428 and never an
envelope. Every case builds its own app from an explicit `DocsSettings`, because the
module-level app in tests/conftest.py was built from whatever environment the suite
started with, exactly as tests/test_media_storage_health.py does for its crypto cases.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import DocsSettings
from app.crypto.config import ENCRYPTED_CONTENT_TYPE, CryptoSettings
from app.crypto.middleware import SESSION_DETAIL
from app.main import create_app
from app.routers.media import INVALID_UPLOAD_TOKEN_DETAIL
from app.security import INVALID_MEDIA_TOKEN_DETAIL

DOCS_URL = "/docs"
REDOC_URL = "/redoc"
OPENAPI_URL = "/openapi.json"
CUSTOM_DOCS_URL = "/swagger"
CUSTOM_REDOC_URL = "/api-docs"
CUSTOM_OPENAPI_URL = "/spec.json"
CITIES_URL = "/api/cities"
PRESIGN_URL = "/api/media/presign"
MOVIE_MEDIA_URL = "/api/movies/{movie_id}/media"
PRESIGN_UPLOAD_URL = "/api/media/{asset_id}/upload"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
DOCS_ENV_NAMES = ("DOCS_ENABLED", "DOCS_URL", "OPENAPI_URL", "REDOC_URL")


def _build_app(docs: DocsSettings, *, crypto: bool = True) -> FastAPI:
    """An app serving these docs settings, with the encryption layer on or off.

    No private key is supplied, so the key pair is a temporary one generated for the
    process -- enough for the middleware to exist and refuse a request without a session,
    which is all these tests need to prove a route is or is not exempt.
    """
    return create_app(CryptoSettings(enabled=crypto, private_key_pem=None), docs=docs)


def _assert_plain(response) -> None:
    """The answer is not an encrypted envelope."""
    content_type = response.headers.get("content-type", "")
    assert ENCRYPTED_CONTENT_TYPE not in content_type


def _assert_plain_html(response, marker: str) -> None:
    """A 200 HTML page carrying this docs UI's marker, served unencrypted."""
    assert response.status_code == 200
    _assert_plain(response)
    assert marker in response.text.lower()


def _assert_plain_404(response, path: str) -> None:
    """The app's own 404: ProblemDetail JSON, never the middleware's 428."""
    assert response.status_code == 404
    _assert_plain(response)
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["instance"] == path


def _assert_session_required(response, path: str) -> None:
    """A route outside the docs bypass is still refused without an encryption session."""
    assert response.status_code == 428
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Precondition Required"
    assert body["status"] == 428
    assert body["detail"] == SESSION_DETAIL
    assert body["instance"] == path


def test_docs_routes_are_plain_and_reachable_with_crypto_enabled() -> None:
    """All three answer 200 plaintext with the encryption layer on and no session."""
    application = _build_app(DocsSettings())
    # The resolved settings are on app.state, exactly as the other settings classes are.
    assert application.state.docs_settings == DocsSettings()

    with TestClient(application) as docs_client:
        swagger = docs_client.get(DOCS_URL, headers={})
        redoc = docs_client.get(REDOC_URL, headers={})
        openapi = docs_client.get(OPENAPI_URL, headers={})

    # No session was ever opened, so nothing here carried an X-Enc-Session header.
    for response in (swagger, redoc, openapi):
        assert "x-enc-session" not in {
            name.lower() for name in response.request.headers
        }

    _assert_plain_html(swagger, "swagger")
    _assert_plain_html(redoc, "redoc")

    assert openapi.status_code == 200
    _assert_plain(openapi)
    spec = openapi.json()
    assert "paths" in spec
    # The spec is still generated from the routers, so every registered route is in it.
    assert "post" in spec["paths"][PRESIGN_URL]
    assert "post" in spec["paths"]["/api/events/kafka"]


def test_docs_routes_return_404_when_docs_are_disabled() -> None:
    """Documentation switched off registers no route, so each path is a plain 404."""
    application = _build_app(DocsSettings(enabled=False))

    with TestClient(application) as docs_client:
        responses = {
            path: docs_client.get(path, headers={})
            for path in (DOCS_URL, REDOC_URL, OPENAPI_URL)
        }

    for path, response in responses.items():
        _assert_plain_404(response, path)


def test_custom_docs_paths_are_served_and_exempt_from_crypto() -> None:
    """The configured paths are the ones served, and the ones the middleware exempts."""
    custom = DocsSettings(
        docs_url=CUSTOM_DOCS_URL,
        openapi_url=CUSTOM_OPENAPI_URL,
        redoc_url=CUSTOM_REDOC_URL,
    )
    application = _build_app(custom)

    with TestClient(application) as docs_client:
        swagger = docs_client.get(CUSTOM_DOCS_URL, headers={})
        redoc = docs_client.get(CUSTOM_REDOC_URL, headers={})
        openapi = docs_client.get(CUSTOM_OPENAPI_URL, headers={})
        # Only the three configured paths are exempt, matched exactly, so the defaults
        # never registered a route: each is the app's own plain 404, not a 428.
        defaults = {
            path: docs_client.get(path, headers={})
            for path in (DOCS_URL, REDOC_URL, OPENAPI_URL)
        }

    _assert_plain_html(swagger, "swagger")
    _assert_plain_html(redoc, "redoc")
    assert openapi.status_code == 200
    _assert_plain(openapi)
    assert "paths" in openapi.json()

    for path, response in defaults.items():
        _assert_plain_404(response, path)

    # The same custom settings on a crypto-free app: the defaults are simply not there.
    plain_application = _build_app(custom, crypto=False)

    with TestClient(plain_application) as plain_client:
        unregistered = {
            path: plain_client.get(path, headers={})
            for path in (DOCS_URL, REDOC_URL, OPENAPI_URL)
        }

    for path, response in unregistered.items():
        _assert_plain_404(response, path)


def test_docs_settings_are_read_from_the_environment(monkeypatch) -> None:
    """DOCS_ENABLED and the three paths come from the environment; blanks fall back."""
    for name in DOCS_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    defaults = DocsSettings.from_env()

    assert defaults == DocsSettings()
    assert (
        defaults.enabled,
        defaults.docs_url,
        defaults.openapi_url,
        defaults.redoc_url,
    ) == (True, DOCS_URL, OPENAPI_URL, REDOC_URL)
    # The three configured paths are exempt whether or not documentation is served.
    assert defaults.bypass_paths() == frozenset({DOCS_URL, OPENAPI_URL, REDOC_URL})

    # Any casing and surrounding whitespace are honoured, as CRYPTO_ENABLED is.
    monkeypatch.setenv("DOCS_ENABLED", " FALSE ")

    disabled = DocsSettings.from_env()

    assert disabled.enabled is False
    assert disabled.bypass_paths() == frozenset({DOCS_URL, OPENAPI_URL, REDOC_URL})

    monkeypatch.setenv("DOCS_ENABLED", "true")
    monkeypatch.setenv("DOCS_URL", CUSTOM_DOCS_URL)
    monkeypatch.setenv("OPENAPI_URL", CUSTOM_OPENAPI_URL)
    monkeypatch.setenv("REDOC_URL", CUSTOM_REDOC_URL)

    custom = DocsSettings.from_env()

    assert custom == DocsSettings(
        enabled=True,
        docs_url=CUSTOM_DOCS_URL,
        openapi_url=CUSTOM_OPENAPI_URL,
        redoc_url=CUSTOM_REDOC_URL,
    )
    assert custom.bypass_paths() == frozenset(
        {CUSTOM_DOCS_URL, CUSTOM_OPENAPI_URL, CUSTOM_REDOC_URL}
    )

    # A trailing slash is stripped, so the registered path and the exempt path are the
    # same string; a blank value keeps the default instead of registering an empty path.
    monkeypatch.setenv("DOCS_URL", f"{CUSTOM_DOCS_URL}/")
    monkeypatch.setenv("REDOC_URL", "   ")

    assert DocsSettings.from_env().docs_url == CUSTOM_DOCS_URL
    assert DocsSettings.from_env().redoc_url == REDOC_URL


def test_non_docs_routes_stay_encrypted() -> None:
    """Only the docs paths were exempted: the API and the media carve-outs are unchanged."""
    application = _build_app(DocsSettings())

    with TestClient(application) as docs_client:
        cities = docs_client.get(CITIES_URL, headers={})
        presign = docs_client.post(PRESIGN_URL, headers={})
        movie_media = docs_client.post(
            MOVIE_MEDIA_URL.format(movie_id=1),
            files={"file": ("asset", b"\x89PNG\r\n\x1a\nposter", "image/png")},
            data={"kind": "POSTER"},
            headers={},
        )
        presigned_put = docs_client.put(
            PRESIGN_UPLOAD_URL.format(asset_id=1),
            content=b"\x89PNG\r\n\x1a\navatar",
            headers={"Content-Type": "image/png"},
        )

    # An ordinary API route still needs an encryption session.
    _assert_session_required(cities, CITIES_URL)
    _assert_session_required(presign, PRESIGN_URL)

    # The multipart upload keeps its own carve-out: its answer is the internal token's
    # 401, plain JSON, not the middleware's 428.
    assert movie_media.status_code == 401
    assert movie_media.headers["content-type"] == "application/json"
    movie_body = movie_media.json()
    assert set(movie_body) == PROBLEM_DETAIL_KEYS
    assert movie_body["status"] == 401
    assert movie_body["detail"] == INVALID_MEDIA_TOKEN_DETAIL
    assert movie_body["instance"] == MOVIE_MEDIA_URL.format(movie_id=1)

    # So does the signed direct upload: the missing token is its own 403, not a 428.
    assert presigned_put.status_code == 403
    assert presigned_put.headers["content-type"] == "application/json"
    put_body = presigned_put.json()
    assert set(put_body) == PROBLEM_DETAIL_KEYS
    assert put_body["status"] == 403
    assert put_body["detail"] == INVALID_UPLOAD_TOKEN_DETAIL
    assert put_body["instance"] == PRESIGN_UPLOAD_URL.format(asset_id=1)
