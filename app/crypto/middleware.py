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
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..errors import problem_detail_content
from .config import CLOCK_SKEW_SECONDS, ENCRYPTED_CONTENT_TYPE
from .envelope import Envelope, open_envelope, request_aad, response_aad, seal
from .keys import ServerKeyPair
from .sessions import Session, SessionStore

PUBLIC_PATHS = frozenset({"/api/crypto/public-key", "/api/crypto/session"})
MEDIA_UPLOAD_PATH = re.compile(r"^/api/movies/\d+/media$")
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


def _path_with_query(scope: Scope) -> str:
    """The `path?query` the client signed; the URL itself stays readable."""
    path = scope.get("path", "")
    query = scope.get("query_string", b"")
    if not query:
        return path
    return f"{path}?{query.decode('latin-1')}"


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
        self, app: ASGIApp, *, keys: ServerKeyPair, sessions: SessionStore
    ) -> None:
        self.app = app
        self.keys = keys
        self.sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        if _is_media_upload(scope):
            # An internal multipart upload: its body is a stream that must not be
            # buffered, and the caller reads the response as plain JSON.
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
