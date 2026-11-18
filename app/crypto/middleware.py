"""Raw ASGI middleware: decrypt request bodies, encrypt response bodies.

This is the Python equivalent of the ticket's ``OncePerRequestFilter``. It sits
outside Starlette's ``ExceptionMiddleware``, so the ProblemDetail bodies the app's
error handlers produce (including the 401 from ``get_current_user``) flow through
it and are encrypted. The middleware's own 415/400/428 answers are built here and
returned unencrypted, because there is no session key to encrypt them with.
"""

import re
import time

from cryptography.exceptions import InvalidTag
from starlette.responses import JSONResponse, Response
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..errors import problem_detail_content
from .config import CLOCK_SKEW_SECONDS, ENCRYPTED_CONTENT_TYPE
from .envelope import Envelope, open_envelope, request_aad, response_aad, seal
from .keys import ServerKeyPair
from .sessions import Session, SessionStore

PUBLIC_PATHS = frozenset(
    {
        "/api/crypto/public-key",
        "/api/crypto/session",
        # The storage health report is public and unauthenticated, so it has to read
        # without an encryption session too: it is the operator's first probe of a
        # deployment, before any client handshake could exist.
        "/api/media/health/storage",
    }
)
INTERNAL_PREFIX = "/internal/"
MEDIA_UPLOAD_PATH = re.compile(r"^/api/movies/\d+/media$")
REVIEW_PHOTO_UPLOAD_PATH = re.compile(r"^/api/movies/\d+/reviews/\d+/photo$")
PRESIGN_UPLOAD_PATH = re.compile(r"^/api/media/\d+/upload$")
PLAIN_BODY_DETAIL = "Encrypted body required"
DECRYPT_DETAIL = "Request could not be decrypted"
SESSION_DETAIL = "Encryption session missing or expired"

TIMESTAMP_TOLERANCE_MS = CLOCK_SKEW_SECONDS * 1000

PLAIN_JSON_CONTENT_TYPE = b"application/json"


class _RequestRejected(Exception):
    """A request the encryption layer refuses; answered plainly, never encrypted."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def plain_problem_response(status_code: int, detail: str, path: str) -> Response:
    """The unencrypted ProblemDetail answer the encryption layer uses."""
    return JSONResponse(
        status_code=status_code,
        content=problem_detail_content(status_code, detail, path),
    )


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _is_media_upload(scope: Scope) -> bool:
    """A plain `POST /api/movies/{id}/media`: another service's raw multipart upload."""
    if scope.get("type") != "http" or scope.get("method") != "POST":
        return False
    return MEDIA_UPLOAD_PATH.fullmatch(scope.get("path", "")) is not None


def _is_review_photo_upload(scope: Scope) -> bool:
    """A plain `POST /api/movies/{id}/reviews/{id}/photo`: the app's multipart upload."""
    if scope.get("type") != "http" or scope.get("method") != "POST":
        return False
    return REVIEW_PHOTO_UPLOAD_PATH.fullmatch(scope.get("path", "")) is not None


def _is_presign_upload(scope: Scope) -> bool:
    """A plain `PUT /api/media/{id}/upload`: the signed direct upload, no session.

    The signed `token` in the query string is the authorization here, so the body is
    raw bytes rather than an encrypted envelope. Only this one `PUT` is bypassed;
    every other `/api/media` route stays encrypted.
    """
    if scope.get("type") != "http" or scope.get("method") != "PUT":
        return False
    return PRESIGN_UPLOAD_PATH.fullmatch(scope.get("path", "")) is not None


def _path_with_query(scope: Scope) -> str:
    """The `path?query` the client signed; the URL itself stays readable."""
    path = scope.get("path", "")
    query = scope.get("query_string", b"")
    if not query:
        return path
    return f"{path}?{query.decode('latin-1')}"


def _is_unserved(scope: Scope) -> bool:
    """Whether no route of this app matches the request at all.

    A path nothing serves has no route to hand a session to and no body to protect, so
    answering it with a 428 would hide the app's own ProblemDetail 404 -- the answer an
    unregistered docs path gets, with or without the encryption layer. Anything that
    matches a route stays guarded, including a partial match for the wrong method.
    """
    application = scope.get("app")
    routes = getattr(application, "routes", None)
    if not routes:
        return False
    for route in routes:
        matched = route.matches(scope)
        match = matched[0] if isinstance(matched, tuple) else matched
        if match is not Match.NONE:
            return False
    return True


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay_body(plaintext: bytes) -> Receive:
    """Hand the decrypted JSON downstream as if the client had sent it plainly."""
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": plaintext, "more_body": False}

    return receive


def _plain_body_headers(
    headers: list[tuple[bytes, bytes]], plaintext: bytes
) -> list[tuple[bytes, bytes]]:
    """Rewrite the envelope headers so controllers see an ordinary JSON request."""
    rewritten = [
        (name, value)
        for name, value in headers
        if name.lower() not in {b"content-type", b"content-length"}
    ]
    rewritten.append((b"content-type", PLAIN_JSON_CONTENT_TYPE))
    rewritten.append((b"content-length", str(len(plaintext)).encode("latin-1")))
    return rewritten


class CryptoMiddleware:
    """Hybrid encryption transport: RSA-OAEP handshake, AES-256-GCM bodies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        keys: ServerKeyPair,
        sessions: SessionStore,
        public_paths: frozenset[str] = PUBLIC_PATHS,
    ) -> None:
        self.app = app
        self.keys = keys
        self.sessions = sessions
        # The paths this app serves without an encryption session: `PUBLIC_PATHS` plus
        # whatever `create_app` adds for this deployment, such as the configured docs
        # routes. Matching stays exact-path equality, so a sub-path is not exempt.
        self.public_paths = public_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("path") in self.public_paths
            or scope.get("path", "").startswith(INTERNAL_PREFIX)
            # A path this app serves no route for is not something to guard: it falls
            # through to the app's own plain 404 rather than the middleware's 428.
            or _is_unserved(scope)
        ):
            await self.app(scope, receive, send)
            return

        if (
            _is_media_upload(scope)
            or _is_review_photo_upload(scope)
            or _is_presign_upload(scope)
        ):
            # A multipart upload or the signed direct upload: its body is a stream that
            # must not be buffered, and each route carries its own authorization and
            # answers plain JSON.
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)
        request_iv = ""

        try:
            session = self._resolve_session(scope, has_body=bool(body))
            if body:
                plaintext, request_iv = self._open_request(scope, body, session)
                scope["headers"] = _plain_body_headers(
                    scope.get("headers", []), plaintext
                )
                receive = _replay_body(plaintext)
        except _RequestRejected as rejected:
            response = plain_problem_response(
                rejected.status_code, rejected.detail, scope.get("path", "")
            )
            await response(scope, receive, send)
            return

        started: Message | None = None
        chunks: list[bytes] = []

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = message
                return
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                if started is not None:
                    response_start = started
                    started = None
                    await self._emit(
                        response_start,
                        b"".join(chunks),
                        session.key,
                        request_iv,
                        send,
                    )
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _resolve_session(self, scope: Scope, *, has_body: bool) -> Session:
        headers = scope.get("headers", [])
        if has_body:
            content_type = (
                (_header(headers, b"content-type") or "")
                .split(";")[0]
                .strip()
                .lower()
            )
            if content_type != ENCRYPTED_CONTENT_TYPE:
                raise _RequestRejected(415, PLAIN_BODY_DETAIL)

        session_id = _header(headers, b"x-enc-session")
        session = self.sessions.get(session_id) if session_id else None
        if session is None:
            raise _RequestRejected(428, SESSION_DETAIL)
        return session

    def _open_request(
        self, scope: Scope, body: bytes, session: Session
    ) -> tuple[bytes, str]:
        headers = scope.get("headers", [])
        timestamp = _header(headers, b"x-enc-ts")
        if timestamp is None or not timestamp.strip():
            raise _RequestRejected(400, DECRYPT_DETAIL)
        try:
            timestamp_ms = int(timestamp)
        except ValueError:
            raise _RequestRejected(400, DECRYPT_DETAIL) from None
        if abs(int(time.time() * 1000) - timestamp_ms) > TIMESTAMP_TOLERANCE_MS:
            raise _RequestRejected(400, DECRYPT_DETAIL)

        try:
            envelope = Envelope.model_validate_json(body)
        except ValueError:
            raise _RequestRejected(400, DECRYPT_DETAIL) from None

        if not self.sessions.register_iv(session, envelope.iv):
            raise _RequestRejected(400, DECRYPT_DETAIL)

        aad = request_aad(
            method=scope.get("method", ""),
            path_with_query=_path_with_query(scope),
            timestamp=timestamp,
            session_id=session.id,
        )
        try:
            plaintext = open_envelope(key=session.key, envelope=envelope, aad=aad)
        except (InvalidTag, ValueError):
            raise _RequestRejected(400, DECRYPT_DETAIL) from None
        return plaintext, envelope.iv

    async def _emit(
        self,
        started: Message,
        body: bytes,
        session_key: bytes,
        request_iv: str,
        send: Send,
    ) -> None:
        """Send the buffered response, encrypted unless it carries no body."""
        status_code = int(started["status"])
        headers = [
            (name, value)
            for name, value in started.get("headers", [])
            if name.lower() != b"content-type"
        ]

        payload = b""
        if body:
            envelope = seal(
                key=session_key,
                plaintext=body,
                aad=response_aad(status_code=status_code, request_iv=request_iv),
            )
            payload = envelope.model_dump_json().encode("utf-8")
            headers = [
                (name, value)
                for name, value in headers
                if name.lower() != b"content-length"
            ]
            headers.append((b"content-type", ENCRYPTED_CONTENT_TYPE.encode("latin-1")))
            headers.append((b"content-length", str(len(payload)).encode("latin-1")))

        await send(
            {"type": "http.response.start", "status": status_code, "headers": headers}
        )
        await send({"type": "http.response.body", "body": payload, "more_body": False})
