"""A review photo's SHA-256: stored on upload, re-checked from storage on demand.

Every upload goes through `POST /api/reviews/{reviewId}/media` into the fresh
MEDIA_UPLOAD_DIR tests/conftest.py points at, so the bytes the integrity and verify
routes re-hash are the ones the upload just wrote. Tampering is simulated by writing
different bytes straight to the file the MediaAsset row's storage_key resolves to,
which is the only way to make a recompute-from-storage disagree with the digest the
row recorded at upload.

The public review card is exercised without a live ratings service: `httpx.get` is
swapped for a recorder answering one review whose id matches the uploaded photo's
review_id, and app.state.reviews_settings points at a URL that is never contacted.
"""

import hashlib
from pathlib import Path

import httpx
import pytest

from app.db import SessionLocal
from app.main import app
from app.media import DEFAULT_HASH_ALGORITHM, MediaSettings
from app.models import MediaAsset
from app.reviews_client import ReviewsSettings
from app.security import MEDIA_SERVICE_TOKEN

UPLOAD_URL = "/api/reviews/{review_id}/media"
INTEGRITY_URL = "/api/media/{asset_id}/integrity"
VERIFY_URL = "/api/media/{asset_id}/verify"
MOVIE_REVIEWS_URL = "/api/movies/{movie_id}/reviews"
TOKEN_HEADER = "X-Internal-Token"
REVIEWS_BASE_URL = "http://reviews.test"
SERVICE_TOKEN = "shared-token"
MOVIE_ID = 1
REVIEW_ID = 7
PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\nreview-photo-bytes"
TAMPERED_PAYLOAD = b"\x89PNG\r\n\x1a\ntampered-review-photo-bytes"
ASSET_KEYS = {"assetId", "kind", "url", "size", "contentType", "sha256"}
INTEGRITY_KEYS = {"sha256", "size", "verified"}
VERIFY_KEYS = {"match"}
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
INVALID_TOKEN_DETAIL = "Invalid media service token"
OTHER_DIGEST = "0" * 64


def _upload_photo(
    client,
    review_id: int = REVIEW_ID,
    payload: bytes = PHOTO_PAYLOAD,
    content_type: str = "image/png",
    token: str | None = MEDIA_SERVICE_TOKEN,
) -> httpx.Response:
    """One multipart review-photo upload; the token header is left off when it is None."""
    headers = {} if token is None else {TOKEN_HEADER: token}
    return client.post(
        UPLOAD_URL.format(review_id=review_id),
        files={"file": ("photo", payload, content_type)},
        headers=headers,
    )


def _integrity(
    client, asset_id: int, token: str | None = MEDIA_SERVICE_TOKEN
) -> httpx.Response:
    headers = {} if token is None else {TOKEN_HEADER: token}
    return client.get(INTEGRITY_URL.format(asset_id=asset_id), headers=headers)


def _verify(
    client,
    asset_id: int,
    sha256: str,
    token: str | None = MEDIA_SERVICE_TOKEN,
) -> httpx.Response:
    headers = {} if token is None else {TOKEN_HEADER: token}
    return client.post(
        VERIFY_URL.format(asset_id=asset_id),
        json={"sha256": sha256},
        headers=headers,
    )


def _stored_asset(asset_id: int) -> MediaAsset:
    """The MediaAsset row, read the way the routes read it."""
    with SessionLocal() as db:
        asset = db.get(MediaAsset, asset_id)
    assert asset is not None
    return asset


def _stored_path(asset: MediaAsset) -> Path:
    return Path(app.state.media_settings.upload_dir) / asset.storage_key


def _tamper(asset: MediaAsset) -> None:
    """Change the bytes behind the row without touching the row itself."""
    _stored_path(asset).write_bytes(TAMPERED_PAYLOAD)


class _Recorder:
    """Stand-in for httpx.get: records each URL and answers the recorded page."""

    def __init__(self, body: object) -> None:
        self.body = body
        self.calls: list[str] = []

    def __call__(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append(url)
        return httpx.Response(
            status_code=200,
            json=self.body,
            request=httpx.Request("GET", url),
        )


def _reviews_page(review_id: int) -> dict[str, object]:
    """The ratings service's answer: one review, whose id the photo is attached to."""
    return {
        "summary": {"average": 5.0, "count": 1, "distribution": {"5": 1}},
        "reviews": [
            {
                "id": review_id,
                "movieId": MOVIE_ID,
                "userId": 42,
                "bookingId": 501,
                "rating": 5,
                "comment": "Loved it",
                "verified": True,
                "createdAt": "2026-10-20T10:00:00+05:30",
            }
        ],
        "page": 1,
        "pageSize": 10,
        "total": 1,
    }


def test_review_photo_upload_stores_the_sha256(client) -> None:
    digest = hashlib.sha256(PHOTO_PAYLOAD).hexdigest()

    response = _upload_photo(client)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == ASSET_KEYS
    assert body["kind"] == "REVIEW_PHOTO"
    assert body["contentType"] == "image/png"
    assert body["size"] == len(PHOTO_PAYLOAD)
    assert body["sha256"] == digest
    assert body["url"].startswith("/media/review-photos/")

    # The url the route returned really serves the bytes it hashed.
    fetched = client.get(body["url"])
    assert fetched.status_code == 200
    assert fetched.content == PHOTO_PAYLOAD

    asset = _stored_asset(body["assetId"])
    assert asset.review_id == REVIEW_ID
    assert asset.movie_id is None
    assert asset.kind == "REVIEW_PHOTO"
    assert asset.sha256 == digest
    assert asset.size_bytes == len(PHOTO_PAYLOAD)
    assert asset.is_current is True
    assert _stored_path(asset).read_bytes() == PHOTO_PAYLOAD


def test_integrity_reports_the_stored_digest_and_recomputes(client) -> None:
    digest = hashlib.sha256(PHOTO_PAYLOAD).hexdigest()
    uploaded = _upload_photo(client)
    assert uploaded.status_code == 201
    asset_id = uploaded.json()["assetId"]

    before = _integrity(client, asset_id)

    assert before.status_code == 200
    body = before.json()
    assert set(body) == INTEGRITY_KEYS
    assert body == {"sha256": digest, "size": len(PHOTO_PAYLOAD), "verified": True}

    _tamper(_stored_asset(asset_id))

    after = _integrity(client, asset_id)

    assert after.status_code == 200
    tampered_body = after.json()
    assert set(tampered_body) == INTEGRITY_KEYS
    # The recorded digest and size stay visible; only `verified` turns false.
    assert tampered_body == {
        "sha256": digest,
        "size": len(PHOTO_PAYLOAD),
        "verified": False,
    }
    assert _stored_asset(asset_id).sha256 == digest


def test_verify_matches_the_recorded_digest(client) -> None:
    digest = hashlib.sha256(PHOTO_PAYLOAD).hexdigest()
    uploaded = _upload_photo(client)
    assert uploaded.status_code == 201
    asset_id = uploaded.json()["assetId"]

    matched = _verify(client, asset_id, digest)

    assert matched.status_code == 200
    assert set(matched.json()) == VERIFY_KEYS
    assert matched.json() == {"match": True}

    # The submitted digest is normalised, so the same digest in uppercase matches too.
    upper = _verify(client, asset_id, digest.upper())

    assert upper.status_code == 200
    assert upper.json() == {"match": True}

    different = _verify(client, asset_id, OTHER_DIGEST)

    assert different.status_code == 200
    assert set(different.json()) == VERIFY_KEYS
    assert different.json() == {"match": False}


def test_tampered_storage_fails_verification(client) -> None:
    digest = hashlib.sha256(PHOTO_PAYLOAD).hexdigest()
    uploaded = _upload_photo(client)
    assert uploaded.status_code == 201
    asset_id = uploaded.json()["assetId"]

    _tamper(_stored_asset(asset_id))

    # The originally correct digest no longer matches the bytes in storage.
    original = _verify(client, asset_id, digest)

    assert original.status_code == 200
    assert original.json() == {"match": False}

    tampered_digest = _verify(
        client, asset_id, hashlib.sha256(TAMPERED_PAYLOAD).hexdigest()
    )

    assert tampered_digest.status_code == 200
    assert tampered_digest.json() == {"match": False}

    assert _integrity(client, asset_id).json()["verified"] is False


def test_public_review_card_hides_a_tampered_photo(client, monkeypatch) -> None:
    digest = hashlib.sha256(PHOTO_PAYLOAD).hexdigest()
    uploaded = _upload_photo(client)
    assert uploaded.status_code == 201
    asset_id = uploaded.json()["assetId"]

    recorder = _Recorder(_reviews_page(REVIEW_ID))
    monkeypatch.setattr(httpx, "get", recorder)
    monkeypatch.setattr(
        app.state,
        "reviews_settings",
        ReviewsSettings(
            base_url=REVIEWS_BASE_URL,
            service_token=SERVICE_TOKEN,
            timeout_seconds=5.0,
        ),
        raising=False,
    )
    url = MOVIE_REVIEWS_URL.format(movie_id=MOVIE_ID)

    trusted = client.get(url)

    assert trusted.status_code == 200
    trusted_reviews = trusted.json()["reviews"]
    assert len(trusted_reviews) == 1
    assert trusted_reviews[0]["photo"] == {
        "assetId": asset_id,
        "sha256": digest,
        "verified": True,
    }

    _tamper(_stored_asset(asset_id))

    untrusted = client.get(url)

    assert untrusted.status_code == 200
    untrusted_reviews = untrusted.json()["reviews"]
    # The review stays listed; only its photo is dropped from the public card.
    assert len(untrusted_reviews) == 1
    assert untrusted_reviews[0]["id"] == REVIEW_ID
    assert untrusted_reviews[0]["rating"] == 5
    assert untrusted_reviews[0]["comment"] == "Loved it"
    assert untrusted_reviews[0]["photo"] is None
    # The row still records the digest it was uploaded with.
    assert _stored_asset(asset_id).sha256 == digest
    # Both reads asked the ratings service, so the photo really was joined locally.
    expected_url = (
        f"{REVIEWS_BASE_URL}/internal/reviews?movieId={MOVIE_ID}&page=1&pageSize=10"
    )
    assert recorder.calls == [expected_url, expected_url]


@pytest.mark.parametrize("token", [None, "not-the-token"])
def test_the_internal_token_is_required_for_integrity_and_verify(
    client, token: str | None
) -> None:
    digest = hashlib.sha256(PHOTO_PAYLOAD).hexdigest()
    uploaded = _upload_photo(client)
    assert uploaded.status_code == 201
    asset_id = uploaded.json()["assetId"]

    integrity = _integrity(client, asset_id, token)

    assert integrity.status_code == 401
    integrity_body = integrity.json()
    assert set(integrity_body) == PROBLEM_DETAIL_KEYS
    assert integrity_body["type"] == "about:blank"
    assert integrity_body["title"] == "Unauthorized"
    assert integrity_body["status"] == 401
    assert integrity_body["detail"] == INVALID_TOKEN_DETAIL
    assert integrity_body["instance"] == INTEGRITY_URL.format(asset_id=asset_id)

    verified = _verify(client, asset_id, digest, token)

    assert verified.status_code == 401
    verify_body = verified.json()
    assert set(verify_body) == PROBLEM_DETAIL_KEYS
    assert verify_body["status"] == 401
    assert verify_body["detail"] == INVALID_TOKEN_DETAIL
    assert verify_body["instance"] == VERIFY_URL.format(asset_id=asset_id)


def test_the_hash_algorithm_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.delenv("MEDIA_HASH_ALGORITHM", raising=False)

    assert DEFAULT_HASH_ALGORITHM == "sha256"
    assert MediaSettings.from_env().hash_algorithm == "sha256"

    for raw in ("", "  ", "md5", "sha512"):
        monkeypatch.setenv("MEDIA_HASH_ALGORITHM", raw)
        assert MediaSettings.from_env().hash_algorithm == "sha256"

    monkeypatch.setenv("MEDIA_HASH_ALGORITHM", "SHA256")
    assert MediaSettings.from_env().hash_algorithm == "sha256"
