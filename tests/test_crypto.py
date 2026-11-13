"""Encryption-layer tests: handshake, envelope round trip and every rejection path.

`tests/fixtures/webcrypto_handshake.json` holds the cross-language handshake
snapshot: an AES-256 session key wrapped with the server's public key using
RSA-OAEP with SHA-256 for both the digest and MGF1 -- exactly the parameters Web
Crypto's RSA-OAEP-256 uses. Node's Web Crypto writes the same values with

    const pair = await crypto.subtle.generateKey(
      { name: "RSA-OAEP", modulusLength: 3072,
        publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
      true,
      ["wrapKey", "unwrapKey"],
    );
    const sessionKey = await crypto.subtle.generateKey(
      { name: "AES-GCM", length: 256 }, true, ["encrypt", "decrypt"],
    );
    const wrapped = await crypto.subtle.wrapKey(
      "raw", sessionKey, pair.publicKey, { name: "RSA-OAEP" },
    );

The file starts with null placeholders and is filled in from Python (same
parameters) on the first run, so a key wrapped here and a key wrapped by a
browser are interchangeable.
"""

import base64
import hashlib
import json
import secrets
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.crypto.config import (
    ENCRYPTED_CONTENT_TYPE,
    KID,
    PUBLIC_KEY_ALG,
    SESSION_TTL_SECONDS,
    CryptoSettings,
)
from app.crypto.envelope import (
    Envelope,
    open_envelope,
    request_aad,
    response_aad,
    seal,
)
from app.crypto.keys import load_key_pair, oaep_padding
from app.crypto.middleware import (
    DECRYPT_DETAIL,
    PLAIN_BODY_DETAIL,
    SESSION_DETAIL,
    CryptoMiddleware,
)
from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import MediaAsset, User
from app.security import issue_token
from seed import seed_data

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "webcrypto_handshake.json"
GENERATOR_PATH = FIXTURE_PATH.with_name("webcrypto_handshake.mjs")
FIXTURE_FIELDS = ("privateKeyPem", "publicJwk", "aesKeyB64", "wrappedKey")
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}

CITIES_URL = "/api/cities"
OTP_REQUEST_URL = "/api/auth/otp/request"
OTP_PAYLOAD = {"countryCode": "+91", "phone": "9876543210"}
PRESIGN_URL = "/api/media/presign"
PRESIGNED_UPLOAD_URL = "/api/media/{asset_id}/upload"
COMPLETE_URL = "/api/media/{asset_id}/complete"
AVATAR_PAYLOAD = b"\x89PNG\r\n\x1a\navatar-bytes"
PRESIGN_KEYS = {"assetId", "uploadUrl", "method", "expiresIn", "headers"}
COMPLETE_WITH_AVATAR_KEYS = {
    "assetId",
    "url",
    "contentType",
    "size",
    "sha256",
    "kind",
    "verified",
}
UNWRAP_DETAIL = "Request could not be decrypted"
AES_KEY_BYTES = 32
REVIEW_PHOTO_URL = "/api/movies/{movie_id}/reviews/{review_id}/photo"
INTEGRITY_URL = "/api/media/{asset_id}/integrity"
VERIFY_URL = "/api/media/{asset_id}/verify"
MEDIA_MOVIE_ID = 1
REVIEW_ID = 501
VERIFIED_REVIEW_ID = 502
PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\nreview-photo-bytes"


def _b64url_uint(value: str) -> int:
    padded = value + "=" * (-len(value) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(padded), "big")


def _public_key_from_jwk(jwk: dict[str, str]) -> rsa.RSAPublicKey:
    numbers = rsa.RSAPublicNumbers(_b64url_uint(jwk["e"]), _b64url_uint(jwk["n"]))
    return numbers.public_key()


def _generate_webcrypto_handshake() -> dict[str, Any]:
    """Record a handshake produced by Node's Web Crypto, not by Python's own RSA code.

    ``webcrypto_handshake.mjs`` wraps an AES-256 key with ``crypto.subtle`` using
    ``RSA-OAEP``/SHA-256, i.e. exactly what a browser does, so the committed
    snapshot proves cross-language interop rather than Python agreeing with itself.
    """
    if shutil.which("node") is None:
        raise RuntimeError(
            f"Node.js is required to record {FIXTURE_PATH.name}; regenerate it with "
            f"`node {GENERATOR_PATH} > {FIXTURE_PATH}`"
        )
    completed = subprocess.run(
        ["node", str(GENERATOR_PATH)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


@lru_cache(maxsize=1)
def _handshake_fixture() -> dict[str, Any]:
    """The recorded handshake, generated and written back when it is still empty."""
    if FIXTURE_PATH.exists():
        recorded = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        if all(recorded.get(field) for field in FIXTURE_FIELDS):
            return recorded

    generated = _generate_webcrypto_handshake()
    try:
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(json.dumps(generated, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return generated


@pytest.fixture()
def encrypted_client():
    application = create_app(
        CryptoSettings(enabled=True, private_key_pem=_handshake_fixture()["privateKeyPem"])
    )
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_data(db)

    with TestClient(application) as test_client:
        yield test_client

    Base.metadata.drop_all(bind=engine)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _session_headers(session_id: str, timestamp_ms: int | None = None) -> dict[str, str]:
    stamp = _now_ms() if timestamp_ms is None else timestamp_ms
    return {"X-Enc-Session": session_id, "X-Enc-Ts": str(stamp)}


def handshake(client: TestClient) -> tuple[str, bytes]:
    """Fetch the public key, wrap a fresh AES-256 key and open a session."""
    published = client.get("/api/crypto/public-key")
    assert published.status_code == 200
    body = published.json()
    assert body["kid"] == KID

    aes_key = secrets.token_bytes(AES_KEY_BYTES)
    wrapped = _public_key_from_jwk(body["jwk"]).encrypt(aes_key, oaep_padding())

    opened = client.post(
        "/api/crypto/session",
        json={
            "kid": body["kid"],
            "wrappedKey": base64.b64encode(wrapped).decode("ascii"),
        },
    )
    assert opened.status_code == 201
    return opened.json()["sessionId"], aes_key


def seal_request(
    client: TestClient,
    session_id: str,
    aes_key: bytes,
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    *,
    timestamp_ms: int | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Build the headers and envelope a browser would send for this request."""
    stamp = _now_ms() if timestamp_ms is None else timestamp_ms
    timestamp = str(stamp)
    envelope = seal(
        key=aes_key,
        plaintext=json.dumps(payload).encode("utf-8"),
        aad=request_aad(
            method=method,
            path_with_query=url,
            timestamp=timestamp,
            session_id=session_id,
        ),
    )
    headers = {
        "Content-Type": ENCRYPTED_CONTENT_TYPE,
        "X-Enc-Session": session_id,
        "X-Enc-Ts": timestamp,
    }
    return headers, envelope.model_dump()


def open_response(
    aes_key: bytes, status_code: int, request_iv: str, envelope: dict[str, Any]
) -> Any:
    """Decrypt a response envelope with the AAD the server should have used."""
    plaintext = open_envelope(
        key=aes_key,
        envelope=Envelope.model_validate(envelope),
        aad=response_aad(status_code=status_code, request_iv=request_iv),
    )
    return json.loads(plaintext)


def _send(
    client: TestClient,
    method: str,
    url: str,
    headers: dict[str, str],
    envelope: dict[str, Any] | None = None,
) -> httpx.Response:
    if envelope is None:
        return client.request(method, url, headers=headers)
    return client.request(method, url, content=json.dumps(envelope), headers=headers)


def test_encrypted_round_trip_through_otp_request(encrypted_client: TestClient) -> None:
    session_id, aes_key = handshake(encrypted_client)
    headers, envelope = seal_request(
        encrypted_client, session_id, aes_key, "POST", OTP_REQUEST_URL, OTP_PAYLOAD
    )

    response = _send(encrypted_client, "POST", OTP_REQUEST_URL, headers, envelope)

    assert response.status_code == 202
    assert response.headers["content-type"] == ENCRYPTED_CONTENT_TYPE
    body = open_response(aes_key, 202, envelope["iv"], response.json())
    assert set(body) == {"challengeId", "resendAfterSeconds", "expiresInSeconds"}
    assert body["resendAfterSeconds"] == 30
    assert body["expiresInSeconds"] == 300
    assert len(body["challengeId"]) == 36


def _user_token(
    phone: str = "+919876543210", name: str | None = None
) -> tuple[int, str]:
    """Insert a signed-in moviegoer straight into the database and mint their JWT."""
    with SessionLocal() as db:
        user = User(phone=phone, name=name)
        db.add(user)
        db.commit()
        return user.id, issue_token(user_id=user.id)


def test_encrypted_put_me_renames_and_returns_an_encrypted_user(
    encrypted_client: TestClient,
) -> None:
    session_id, aes_key = handshake(encrypted_client)
    user_id, token = _user_token()

    headers, envelope = seal_request(
        encrypted_client, session_id, aes_key, "PUT", "/api/me", {"name": "Kshitija"}
    )
    headers["Authorization"] = f"Bearer {token}"

    response = _send(encrypted_client, "PUT", "/api/me", headers, envelope)

    assert response.status_code == 200
    assert response.headers["content-type"] == ENCRYPTED_CONTENT_TYPE
    assert "Kshitija" not in response.text
    assert b"Kshitija" not in response.content

    body = open_response(aes_key, 200, envelope["iv"], response.json())
    assert set(body) == {"id", "phone", "name"}
    assert body["id"] == user_id
    assert body["name"] == "Kshitija"


def test_plain_json_put_me_is_415(encrypted_client: TestClient) -> None:
    _, token = _user_token(phone="+919876543211")

    response = encrypted_client.put(
        "/api/me",
        json={"name": "Test"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 415
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Unsupported Media Type"
    assert body["status"] == 415
    assert body["detail"] == PLAIN_BODY_DETAIL
    assert body["instance"] == "/api/me"
    assert response.headers["content-type"] == "application/json"


def test_bodyless_get_returns_an_encrypted_response(
    encrypted_client: TestClient,
) -> None:
    session_id, aes_key = handshake(encrypted_client)

    response = _send(encrypted_client, "GET", CITIES_URL, _session_headers(session_id))

    assert response.status_code == 200
    assert response.headers["content-type"] == ENCRYPTED_CONTENT_TYPE
    cities = open_response(aes_key, 200, "", response.json())
    assert cities
    assert {city["slug"] for city in cities} >= {"mumbai"}


def test_zero_length_response_has_no_body_or_content_type(
    encrypted_client: TestClient,
) -> None:
    session_id, aes_key = handshake(encrypted_client)
    headers, envelope = seal_request(
        encrypted_client,
        session_id,
        aes_key,
        "POST",
        CITIES_URL,
        {"name": "Nagpur", "state": "Maharashtra"},
    )

    created = _send(encrypted_client, "POST", CITIES_URL, headers, envelope)

    assert created.status_code == 201
    city = open_response(aes_key, 201, envelope["iv"], created.json())
    assert city["slug"] == "nagpur"

    deleted = _send(
        encrypted_client,
        "DELETE",
        f"{CITIES_URL}/{city['id']}",
        _session_headers(session_id),
    )

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert "content-type" not in deleted.headers


def test_tampered_ciphertext_is_400(encrypted_client: TestClient) -> None:
    session_id, aes_key = handshake(encrypted_client)
    headers, envelope = seal_request(
        encrypted_client, session_id, aes_key, "POST", OTP_REQUEST_URL, OTP_PAYLOAD
    )
    raw = bytearray(base64.b64decode(envelope["ct"]))
    raw[0] ^= 0x01
    tampered = dict(envelope, ct=base64.b64encode(bytes(raw)).decode("ascii"))

    response = _send(encrypted_client, "POST", OTP_REQUEST_URL, headers, tampered)

    assert response.status_code == 400
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Bad Request"
    assert body["status"] == 400
    assert body["detail"] == DECRYPT_DETAIL
    assert body["instance"] == OTP_REQUEST_URL
    assert response.headers["content-type"] == "application/json"


def test_body_moved_to_another_path_or_method_is_400(
    encrypted_client: TestClient,
) -> None:
    session_id, aes_key = handshake(encrypted_client)
    headers, envelope = seal_request(
        encrypted_client, session_id, aes_key, "POST", OTP_REQUEST_URL, OTP_PAYLOAD
    )

    moved = _send(encrypted_client, "POST", CITIES_URL, headers, envelope)

    assert moved.status_code == 400
    assert moved.json()["detail"] == DECRYPT_DETAIL

    city_headers, city_envelope = seal_request(
        encrypted_client,
        session_id,
        aes_key,
        "POST",
        CITIES_URL,
        {"name": "Pune", "state": "Maharashtra"},
    )
    rewritten = _send(
        encrypted_client, "PUT", f"{CITIES_URL}/1", city_headers, city_envelope
    )

    assert rewritten.status_code == 400
    assert rewritten.json()["detail"] == DECRYPT_DETAIL


def test_reused_iv_is_400(encrypted_client: TestClient) -> None:
    session_id, aes_key = handshake(encrypted_client)
    headers, envelope = seal_request(
        encrypted_client, session_id, aes_key, "POST", OTP_REQUEST_URL, OTP_PAYLOAD
    )

    accepted = _send(encrypted_client, "POST", OTP_REQUEST_URL, headers, envelope)
    replayed = _send(encrypted_client, "POST", OTP_REQUEST_URL, headers, envelope)

    assert accepted.status_code == 202
    assert replayed.status_code == 400
    body = replayed.json()
    assert body["title"] == "Bad Request"
    assert body["detail"] == DECRYPT_DETAIL


def test_stale_and_future_timestamps_are_400(encrypted_client: TestClient) -> None:
    session_id, aes_key = handshake(encrypted_client)
    now_ms = _now_ms()

    recent = seal_request(
        encrypted_client,
        session_id,
        aes_key,
        "POST",
        OTP_REQUEST_URL,
        OTP_PAYLOAD,
        timestamp_ms=now_ms - 59_000,
    )
    fresh = _send(encrypted_client, "POST", OTP_REQUEST_URL, recent[0], recent[1])
    assert fresh.status_code == 202

    stale = seal_request(
        encrypted_client,
        session_id,
        aes_key,
        "POST",
        OTP_REQUEST_URL,
        OTP_PAYLOAD,
        timestamp_ms=now_ms - 61_000,
    )
    behind = _send(encrypted_client, "POST", OTP_REQUEST_URL, stale[0], stale[1])

    assert behind.status_code == 400
    assert behind.json()["detail"] == DECRYPT_DETAIL

    ahead_envelope = seal_request(
        encrypted_client,
        session_id,
        aes_key,
        "POST",
        OTP_REQUEST_URL,
        OTP_PAYLOAD,
        timestamp_ms=now_ms + 61_000,
    )
    ahead = _send(
        encrypted_client, "POST", OTP_REQUEST_URL, ahead_envelope[0], ahead_envelope[1]
    )

    assert ahead.status_code == 400
    assert ahead.json()["detail"] == DECRYPT_DETAIL


def test_expired_session_is_428(encrypted_client: TestClient) -> None:
    session_id, aes_key = handshake(encrypted_client)
    sessions = encrypted_client.app.state.crypto_sessions
    session = sessions.get(session_id)
    assert session is not None
    session.expires_at = time.time() - 1

    headers, envelope = seal_request(
        encrypted_client, session_id, aes_key, "POST", OTP_REQUEST_URL, OTP_PAYLOAD
    )
    response = _send(encrypted_client, "POST", OTP_REQUEST_URL, headers, envelope)

    assert response.status_code == 428
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Precondition Required"
    assert body["status"] == 428
    assert body["detail"] == SESSION_DETAIL
    assert body["instance"] == OTP_REQUEST_URL
    assert response.headers["content-type"] == "application/json"


def test_missing_or_unknown_session_is_428(encrypted_client: TestClient) -> None:
    missing = encrypted_client.get(CITIES_URL)

    assert missing.status_code == 428
    assert missing.json()["detail"] == SESSION_DETAIL

    unknown = encrypted_client.get(
        CITIES_URL, headers=_session_headers("no-such-session")
    )

    assert unknown.status_code == 428
    assert unknown.json()["detail"] == SESSION_DETAIL
    assert unknown.json()["title"] == "Precondition Required"


def test_plain_json_body_is_415(encrypted_client: TestClient) -> None:
    response = encrypted_client.post(OTP_REQUEST_URL, json=OTP_PAYLOAD)

    assert response.status_code == 415
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Unsupported Media Type"
    assert body["status"] == 415
    assert body["detail"] == PLAIN_BODY_DETAIL
    assert body["instance"] == OTP_REQUEST_URL
    assert "iv" not in body
    assert response.headers["content-type"] == "application/json"


def test_crypto_endpoints_stay_public(encrypted_client: TestClient) -> None:
    published = encrypted_client.get("/api/crypto/public-key")

    assert published.status_code == 200
    assert published.headers["content-type"] == "application/json"
    key_body = published.json()
    assert set(key_body) == {"kid", "alg", "jwk"}
    assert key_body["kid"] == KID == "2026-10-a"
    assert key_body["alg"] == PUBLIC_KEY_ALG == "RSA-OAEP-256"
    assert key_body["jwk"]["kty"] == "RSA"
    assert key_body["jwk"]["n"]
    assert key_body["jwk"]["e"]

    aes_key = secrets.token_bytes(AES_KEY_BYTES)
    wrapped = _public_key_from_jwk(key_body["jwk"]).encrypt(aes_key, oaep_padding())
    opened = encrypted_client.post(
        "/api/crypto/session",
        json={
            "kid": key_body["kid"],
            "wrappedKey": base64.b64encode(wrapped).decode("ascii"),
        },
    )

    assert opened.status_code == 201
    assert opened.headers["content-type"] == "application/json"
    session_body = opened.json()
    assert set(session_body) == {"sessionId", "expiresInSeconds"}
    assert session_body["sessionId"]
    assert session_body["expiresInSeconds"] == SESSION_TTL_SECONDS == 1800


def test_review_photo_upload_is_plain_multipart_under_encryption(
    encrypted_client: TestClient,
) -> None:
    """The review-photo multipart upload keeps its own carve-out beside the media one."""
    session_id, aes_key = handshake(encrypted_client)
    _, token = _user_token(phone="+919876543220")
    url = REVIEW_PHOTO_URL.format(movie_id=MEDIA_MOVIE_ID, review_id=REVIEW_ID)

    uploaded = encrypted_client.post(
        url,
        files={"file": ("photo", PHOTO_PAYLOAD, "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert uploaded.status_code == 201
    assert uploaded.headers["content-type"].startswith("application/json")
    body = uploaded.json()
    assert set(body) == {"url", "sha256", "verified"}
    assert body["sha256"] == hashlib.sha256(PHOTO_PAYLOAD).hexdigest()
    assert body["verified"] is True

    # The /media mount is an ordinary route, so its bytes come back encrypted too.
    served = _send(encrypted_client, "GET", body["url"], _session_headers(session_id))

    assert served.status_code == 200
    assert served.headers["content-type"] == ENCRYPTED_CONTENT_TYPE
    stored = open_envelope(
        key=aes_key,
        envelope=Envelope.model_validate(served.json()),
        aad=response_aad(status_code=200, request_iv=""),
    )
    assert stored == PHOTO_PAYLOAD


def test_integrity_and_verify_round_trip_through_an_encrypted_session(
    encrypted_client: TestClient,
) -> None:
    """Both JSON routes stay encrypted: no public carve-out, and a plain body is a 415."""
    session_id, aes_key = handshake(encrypted_client)
    _, token = _user_token(phone="+919876543221")
    auth = {"Authorization": f"Bearer {token}"}

    uploaded = encrypted_client.post(
        REVIEW_PHOTO_URL.format(movie_id=MEDIA_MOVIE_ID, review_id=VERIFIED_REVIEW_ID),
        files={"file": ("photo", PHOTO_PAYLOAD, "image/png")},
        headers=auth,
    )
    assert uploaded.status_code == 201
    digest = uploaded.json()["sha256"]

    with SessionLocal() as db:
        asset = db.scalar(
            select(MediaAsset)
            .where(
                MediaAsset.review_id == VERIFIED_REVIEW_ID,
                MediaAsset.kind == "REVIEW_PHOTO",
            )
            .order_by(MediaAsset.id.desc())
        )
        assert asset is not None
        asset_id = asset.id

    integrity_url = INTEGRITY_URL.format(asset_id=asset_id)
    integrity = _send(
        encrypted_client,
        "GET",
        integrity_url,
        {**_session_headers(session_id), **auth},
    )

    assert integrity.status_code == 200
    assert integrity.headers["content-type"] == ENCRYPTED_CONTENT_TYPE
    integrity_body = open_response(aes_key, 200, "", integrity.json())
    assert integrity_body == {
        "sha256": digest,
        "size": len(PHOTO_PAYLOAD),
        "verified": True,
    }

    verify_url = VERIFY_URL.format(asset_id=asset_id)
    headers, envelope = seal_request(
        encrypted_client, session_id, aes_key, "POST", verify_url, {"sha256": digest}
    )
    headers.update(auth)
    verified = _send(encrypted_client, "POST", verify_url, headers, envelope)

    assert verified.status_code == 200
    assert verified.headers["content-type"] == ENCRYPTED_CONTENT_TYPE
    assert open_response(aes_key, 200, envelope["iv"], verified.json()) == {
        "match": True
    }

    plain = encrypted_client.post(verify_url, json={"sha256": digest}, headers=auth)

    assert plain.status_code == 415
    problem = plain.json()
    assert set(problem) == PROBLEM_DETAIL_KEYS
    assert problem["title"] == "Unsupported Media Type"
    assert problem["status"] == 415
    assert problem["detail"] == PLAIN_BODY_DETAIL
    assert problem["instance"] == verify_url


def test_presign_and_complete_stay_encrypted(encrypted_client: TestClient) -> None:
    """Both JSON ends of a direct upload stay inside the envelope, as every route does."""
    session_id, aes_key = handshake(encrypted_client)
    _, token = _user_token(phone="+919876543230")
    auth = {"Authorization": f"Bearer {token}"}
    presign_payload = {
        "purpose": "avatar",
        "filename": "me.png",
        "contentType": "image/png",
        "size": len(AVATAR_PAYLOAD),
    }

    headers, envelope = seal_request(
        encrypted_client, session_id, aes_key, "POST", PRESIGN_URL, presign_payload
    )
    headers.update(auth)

    presigned = _send(encrypted_client, "POST", PRESIGN_URL, headers, envelope)

    assert presigned.status_code == 200
    assert presigned.headers["content-type"] == ENCRYPTED_CONTENT_TYPE
    presign_body = open_response(aes_key, 200, envelope["iv"], presigned.json())
    assert set(presign_body) == PRESIGN_KEYS
    assert presign_body["method"] == "PUT"
    assert presign_body["headers"] == {"Content-Type": "image/png"}
    assert (
        presign_body["expiresIn"]
        == encrypted_client.app.state.media_settings.presign_expires_seconds
    )
    assert presign_body["uploadUrl"].startswith(
        PRESIGNED_UPLOAD_URL.format(asset_id=presign_body["assetId"]) + "?token="
    )

    plain_presign = encrypted_client.post(
        PRESIGN_URL, json=presign_payload, headers=auth
    )

    assert plain_presign.status_code == 415
    plain_body = plain_presign.json()
    assert set(plain_body) == PROBLEM_DETAIL_KEYS
    assert plain_body["title"] == "Unsupported Media Type"
    assert plain_body["status"] == 415
    assert plain_body["detail"] == PLAIN_BODY_DETAIL
    assert plain_body["instance"] == PRESIGN_URL

    # The bytes are the one plain step: the signed url carries its own token, and the
    # client that sends them has no encryption session to seal them with.
    uploaded = encrypted_client.put(
        presign_body["uploadUrl"],
        content=AVATAR_PAYLOAD,
        headers={"Content-Type": "image/png"},
    )

    assert uploaded.status_code == 204
    assert uploaded.content == b""

    complete_url = COMPLETE_URL.format(asset_id=presign_body["assetId"])
    completed = _send(
        encrypted_client, "POST", complete_url, {**_session_headers(session_id), **auth}
    )

    assert completed.status_code == 200
    assert completed.headers["content-type"] == ENCRYPTED_CONTENT_TYPE
    complete_body = open_response(aes_key, 200, "", completed.json())
    assert set(complete_body) == COMPLETE_WITH_AVATAR_KEYS
    assert complete_body["assetId"] == presign_body["assetId"]
    assert complete_body["kind"] == "AVATAR"
    assert complete_body["verified"] is True
    assert complete_body["contentType"] == "image/png"
    assert complete_body["size"] == len(AVATAR_PAYLOAD)
    assert complete_body["sha256"] == hashlib.sha256(AVATAR_PAYLOAD).hexdigest()
    assert complete_body["url"].startswith("/media/avatars/")

    plain_complete = encrypted_client.post(complete_url, json={}, headers=auth)

    assert plain_complete.status_code == 415
    plain_complete_body = plain_complete.json()
    assert set(plain_complete_body) == PROBLEM_DETAIL_KEYS
    assert plain_complete_body["detail"] == PLAIN_BODY_DETAIL
    assert plain_complete_body["instance"] == complete_url


def test_presigned_upload_is_plain_under_encryption(
    encrypted_client: TestClient,
) -> None:
    """The one new route outside the envelope: a browser PUTs bytes without a session."""
    session_id, aes_key = handshake(encrypted_client)
    _, token = _user_token(phone="+919876543231")
    headers, envelope = seal_request(
        encrypted_client,
        session_id,
        aes_key,
        "POST",
        PRESIGN_URL,
        {
            "purpose": "avatar",
            "filename": "me.png",
            "contentType": "image/png",
            "size": len(AVATAR_PAYLOAD),
        },
    )
    headers["Authorization"] = f"Bearer {token}"

    presigned = _send(encrypted_client, "POST", PRESIGN_URL, headers, envelope)

    assert presigned.status_code == 200
    upload_url = open_response(
        aes_key, 200, envelope["iv"], presigned.json()
    )["uploadUrl"]
    assert upload_url.startswith("/api/media/")
    assert "?token=" in upload_url

    # No X-Enc-Session header and no envelope: the token in the url is the whole guard.
    uploaded = encrypted_client.put(
        upload_url,
        content=AVATAR_PAYLOAD,
        headers={"Content-Type": "image/png"},
    )

    assert uploaded.status_code == 204
    assert uploaded.content == b""


def test_wrapped_key_from_the_webcrypto_fixture_opens_a_session(
    encrypted_client: TestClient,
) -> None:
    fixture = _handshake_fixture()

    opened = encrypted_client.post(
        "/api/crypto/session",
        json={"kid": fixture["kid"], "wrappedKey": fixture["wrappedKey"]},
    )

    assert opened.status_code == 201
    session_id = opened.json()["sessionId"]
    aes_key = base64.b64decode(fixture["aesKeyB64"], validate=True)

    headers, envelope = seal_request(
        encrypted_client, session_id, aes_key, "POST", OTP_REQUEST_URL, OTP_PAYLOAD
    )
    accepted = _send(encrypted_client, "POST", OTP_REQUEST_URL, headers, envelope)

    assert accepted.status_code == 202
    body = open_response(aes_key, 202, envelope["iv"], accepted.json())
    assert len(body["challengeId"]) == 36


def test_key_wrapped_with_mgf1_sha1_is_rejected(encrypted_client: TestClient) -> None:
    fixture = _handshake_fixture()
    pair = load_key_pair(
        CryptoSettings(enabled=True, private_key_pem=fixture["privateKeyPem"])
    )
    sha1_oaep = padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA1()),
        algorithm=hashes.SHA256(),
        label=None,
    )
    wrapped = pair.public_key.encrypt(secrets.token_bytes(AES_KEY_BYTES), sha1_oaep)

    response = encrypted_client.post(
        "/api/crypto/session",
        json={
            "kid": fixture["kid"],
            "wrappedKey": base64.b64encode(wrapped).decode("ascii"),
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == UNWRAP_DETAIL


def test_middleware_is_absent_when_crypto_is_disabled() -> None:
    disabled = create_app(CryptoSettings(enabled=False, private_key_pem=None))

    assert all(
        middleware.cls is not CryptoMiddleware for middleware in disabled.user_middleware
    )

    enabled = create_app(
        CryptoSettings(enabled=True, private_key_pem=_handshake_fixture()["privateKeyPem"])
    )

    assert any(
        middleware.cls is CryptoMiddleware for middleware in enabled.user_middleware
    )
