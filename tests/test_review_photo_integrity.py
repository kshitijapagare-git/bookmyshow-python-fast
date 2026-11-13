"""Review-photo hashing, the integrity/verify routes and the public review card.

Every photo is uploaded through the authenticated review-photo route into the fresh
MEDIA_UPLOAD_DIR tests/conftest.py points at, so "tampered" means the bytes under that
directory were rewritten after the upload was accepted, and the routes themselves are
what notice it. The ratings service is faked through the same dependency override
tests/test_movie_reviews.py uses, so the public card can be read without a live
service, and the reviewer is created straight in the database with a token from
`issue_token`, the way those tests authenticate without an OTP round trip.
"""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.media import MediaSettings
from app.models import MediaAsset, User
from app.reviews_client import get_reviews_client
from app.schemas import MediaKind
from app.security import issue_token

PHOTO_URL = "/api/movies/{movie_id}/reviews/{review_id}/photo"
REVIEWS_URL = "/api/movies/{movie_id}/reviews"
INTEGRITY_URL = "/api/media/{asset_id}/integrity"
VERIFY_URL = "/api/media/{asset_id}/verify"
MOVIE_ID = 1
REVIEW_ID = 501
UNKNOWN_ASSET_ID = 999999
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
INTEGRITY_KEYS = {"sha256", "size", "verified"}
PHOTO_KEYS = {"url", "sha256", "verified"}
REVIEW_KEYS = {
    "id",
    "movieId",
    "userId",
    "bookingId",
    "groupId",
    "rating",
    "comment",
    "verified",
    "status",
    "createdAt",
}
PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\nreview-photo-bytes"
SECOND_PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\nother-review-photo-bytes"
TAMPERED_PAYLOAD = b"\x89PNG\r\n\x1a\ntampered-after-upload"


def _upload_photo(
    client,
    movie_id: int,
    review_id: int,
    payload: bytes,
    content_type: str,
    token: str | None = None,
) -> httpx.Response:
    """One multipart upload; the bearer token is left off entirely when it is None."""
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return client.post(
        PHOTO_URL.format(movie_id=movie_id, review_id=review_id),
        files={"file": ("photo", payload, content_type)},
        headers=headers,
    )


def _create_user(phone: str) -> tuple[int, str]:
    """Insert a signed-in moviegoer and mint their JWT, as the review tests do."""
    with SessionLocal() as db:
        user = User(phone=phone, name=None, created_at=datetime.now(timezone.utc))
        db.add(user)
        db.commit()
        return user.id, issue_token(user_id=user.id)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _asset(asset_id: int) -> MediaAsset | None:
    """The row itself, so an assertion can check the persisted `verified` flag."""
    with SessionLocal() as db:
        return db.get(MediaAsset, asset_id)


def _current_photo(review_id: int) -> MediaAsset:
    """The review's current REVIEW_PHOTO row, as the upload route left it."""
    with SessionLocal() as db:
        asset = db.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.review_id == review_id,
                MediaAsset.kind == MediaKind.REVIEW_PHOTO.value,
                MediaAsset.is_current.is_(True),
            )
            .order_by(MediaAsset.id.desc())
        ).first()
    assert asset is not None
    return asset


def _tamper(asset: MediaAsset) -> None:
    """Rewrite the bytes under the row's storage key, keeping the file in place."""
    path = Path(app.state.media_settings.upload_dir) / asset.storage_key
    assert path.is_file()
    path.write_bytes(TAMPERED_PAYLOAD)


class _FakeReviewsService:
    """Stand-in for ReviewsClient: answers one review of the movie being read."""

    def __init__(self, review_id: int = REVIEW_ID) -> None:
        self.review_id = review_id
        self.calls: list[dict[str, int]] = []

    def list_reviews(
        self, *, movie_id: int, page: int, page_size: int
    ) -> dict[str, object]:
        self.calls.append({"movie_id": movie_id, "page": page, "page_size": page_size})
        return {
            "summary": {"average": 4.5, "count": 1, "distribution": {"5": 1}},
            "reviews": [
                {
                    "id": self.review_id,
                    "movieId": movie_id,
                    "userId": 42,
                    "bookingId": None,
                    "groupId": None,
                    "rating": 5,
                    "comment": "Loved it",
                    "verified": True,
                    "status": "ACTIVE",
                    "createdAt": "2026-10-20T10:00:00+05:30",
                }
            ],
            "page": page,
            "pageSize": page_size,
            "total": 1,
        }


@contextmanager
def _use_reviews(fake: _FakeReviewsService) -> Iterator[_FakeReviewsService]:
    """Serve the public card from the fake; the override is always popped again."""
    app.dependency_overrides[get_reviews_client] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_reviews_client, None)


def test_upload_stores_the_sha256_on_the_review_photo(client) -> None:
    _, token = _create_user("+919000004001")

    response = _upload_photo(
        client, MOVIE_ID, REVIEW_ID, PHOTO_PAYLOAD, "image/png", token
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == PHOTO_KEYS
    assert body["sha256"] == hashlib.sha256(PHOTO_PAYLOAD).hexdigest()
    assert body["verified"] is True
    assert body["url"].startswith("/media/review-photos/")

    asset = _current_photo(REVIEW_ID)
    assert asset.movie_id == MOVIE_ID
    assert asset.review_id == REVIEW_ID
    assert asset.kind == MediaKind.REVIEW_PHOTO.value
    assert asset.url == body["url"]
    assert asset.content_type == "image/png"
    assert asset.size_bytes == len(PHOTO_PAYLOAD)
    assert asset.sha256 == body["sha256"]
    assert asset.is_current is True
    assert asset.verified is True

    # The url the upload answered really serves the bytes that were hashed.
    served = client.get(body["url"])
    assert served.status_code == 200
    assert served.content == PHOTO_PAYLOAD


def test_a_second_photo_supersedes_the_first_one(client) -> None:
    _, token = _create_user("+919000004002")
    first = _upload_photo(
        client, MOVIE_ID, REVIEW_ID, PHOTO_PAYLOAD, "image/png", token
    )
    second = _upload_photo(
        client, MOVIE_ID, REVIEW_ID, SECOND_PHOTO_PAYLOAD, "image/png", token
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["url"] != second.json()["url"]
    assert second.json()["sha256"] == hashlib.sha256(SECOND_PHOTO_PAYLOAD).hexdigest()

    current = _current_photo(REVIEW_ID)
    assert current.sha256 == second.json()["sha256"]
    # The superseded photo keeps its row and its file, exactly as a poster does.
    assert client.get(first.json()["url"]).content == PHOTO_PAYLOAD
    assert client.get(second.json()["url"]).content == SECOND_PHOTO_PAYLOAD


def test_integrity_reports_the_stored_digest_and_size(client) -> None:
    _, token = _create_user("+919000004003")
    uploaded = _upload_photo(
        client, MOVIE_ID, REVIEW_ID, PHOTO_PAYLOAD, "image/png", token
    )
    assert uploaded.status_code == 201
    asset = _current_photo(REVIEW_ID)

    response = client.get(
        INTEGRITY_URL.format(asset_id=asset.id), headers=_auth(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == INTEGRITY_KEYS
    assert body["sha256"] == hashlib.sha256(PHOTO_PAYLOAD).hexdigest()
    assert body["size"] == len(PHOTO_PAYLOAD)
    assert body["verified"] is True

    stored = _asset(asset.id)
    assert stored is not None
    assert stored.verified is True
    # Reading the integrity never rewrites what the upload recorded.
    assert stored.sha256 == body["sha256"]
    assert stored.size_bytes == body["size"]


def test_integrity_recomputes_from_storage_after_tampering(client) -> None:
    _, token = _create_user("+919000004004")
    uploaded = _upload_photo(
        client, MOVIE_ID, REVIEW_ID, PHOTO_PAYLOAD, "image/png", token
    )
    assert uploaded.status_code == 201
    asset = _current_photo(REVIEW_ID)
    _tamper(asset)

    response = client.get(
        INTEGRITY_URL.format(asset_id=asset.id), headers=_auth(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == INTEGRITY_KEYS
    # The recorded digest is what the moderator compares against; verified says it moved.
    assert body["sha256"] == hashlib.sha256(PHOTO_PAYLOAD).hexdigest()
    assert body["size"] == len(PHOTO_PAYLOAD)
    assert body["verified"] is False

    stored = _asset(asset.id)
    assert stored is not None
    assert stored.verified is False
    assert stored.sha256 == hashlib.sha256(PHOTO_PAYLOAD).hexdigest()


def test_verify_matches_and_mismatches_the_stored_bytes(client) -> None:
    _, token = _create_user("+919000004005")
    uploaded = _upload_photo(
        client, MOVIE_ID, REVIEW_ID, PHOTO_PAYLOAD, "image/png", token
    )
    assert uploaded.status_code == 201
    digest = uploaded.json()["sha256"]
    asset = _current_photo(REVIEW_ID)
    url = VERIFY_URL.format(asset_id=asset.id)

    matching = client.post(url, json={"sha256": digest}, headers=_auth(token))

    assert matching.status_code == 200
    assert matching.json() == {"match": True}

    stored = _asset(asset.id)
    assert stored is not None
    assert stored.verified is True

    other_digest = hashlib.sha256(b"a different photo entirely").hexdigest()
    mismatched = client.post(
        url, json={"sha256": other_digest}, headers=_auth(token)
    )

    assert mismatched.status_code == 200
    assert mismatched.json() == {"match": False}

    stored = _asset(asset.id)
    assert stored is not None
    assert stored.verified is False

    invalid = client.post(url, json={"sha256": "not-a-digest"}, headers=_auth(token))

    assert invalid.status_code == 422
    body = invalid.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["status"] == 422
    assert body["instance"] == url


def test_a_digest_matching_tampered_bytes_still_leaves_the_photo_untrusted(
    client,
) -> None:
    _, token = _create_user("+919000004006")
    uploaded = _upload_photo(
        client, MOVIE_ID, REVIEW_ID, PHOTO_PAYLOAD, "image/png", token
    )
    assert uploaded.status_code == 201
    asset = _current_photo(REVIEW_ID)
    _tamper(asset)

    response = client.post(
        VERIFY_URL.format(asset_id=asset.id),
        json={"sha256": hashlib.sha256(TAMPERED_PAYLOAD).hexdigest()},
        headers=_auth(token),
    )

    # The submitted digest names the bytes storage holds now, so it matches ...
    assert response.status_code == 200
    assert response.json() == {"match": True}

    # ... but they are not the bytes recorded at upload, so the photo stays untrusted.
    stored = _asset(asset.id)
    assert stored is not None
    assert stored.verified is False
    assert stored.sha256 == hashlib.sha256(PHOTO_PAYLOAD).hexdigest()


def test_a_tampered_photo_is_hidden_from_the_public_review_card(client) -> None:
    _, token = _create_user("+919000004007")
    uploaded = _upload_photo(
        client, MOVIE_ID, REVIEW_ID, PHOTO_PAYLOAD, "image/png", token
    )
    assert uploaded.status_code == 201
    asset = _current_photo(REVIEW_ID)
    fake = _FakeReviewsService()

    with _use_reviews(fake):
        before = client.get(REVIEWS_URL.format(movie_id=MOVIE_ID))

        assert before.status_code == 200
        listed = before.json()["reviews"][0]
        assert listed["id"] == REVIEW_ID
        assert listed["photo"] == {
            "url": uploaded.json()["url"],
            "sha256": hashlib.sha256(PHOTO_PAYLOAD).hexdigest(),
            "verified": True,
        }
        served = client.get(listed["photo"]["url"])
        assert served.status_code == 200
        assert served.content == PHOTO_PAYLOAD

        _tamper(asset)
        integrity = client.get(
            INTEGRITY_URL.format(asset_id=asset.id), headers=_auth(token)
        )
        assert integrity.status_code == 200
        assert integrity.json()["verified"] is False

        after = client.get(REVIEWS_URL.format(movie_id=MOVIE_ID))

    assert after.status_code == 200
    body = after.json()
    assert body["summary"] == {"average": 4.5, "count": 1, "distribution": {"5": 1}}
    assert body["page"] == 1
    assert body["pageSize"] == 10
    assert body["total"] == 1

    review = body["reviews"][0]
    # The review itself is still listed, only its untrusted photo is dropped.
    assert set(review) == REVIEW_KEYS
    assert review["id"] == REVIEW_ID
    assert review["rating"] == 5
    assert review["comment"] == "Loved it"
    assert "photo" not in review

    assert fake.calls == [
        {"movie_id": MOVIE_ID, "page": 1, "page_size": 10},
        {"movie_id": MOVIE_ID, "page": 1, "page_size": 10},
    ]


def test_integrity_and_verify_require_a_bearer_token_and_an_existing_asset(
    client,
) -> None:
    _, token = _create_user("+919000004008")
    uploaded = _upload_photo(
        client, MOVIE_ID, REVIEW_ID, PHOTO_PAYLOAD, "image/png", token
    )
    assert uploaded.status_code == 201
    digest = uploaded.json()["sha256"]
    asset = _current_photo(REVIEW_ID)
    integrity_url = INTEGRITY_URL.format(asset_id=asset.id)
    verify_url = VERIFY_URL.format(asset_id=asset.id)

    unauthenticated = [
        client.get(integrity_url),
        client.post(verify_url, json={"sha256": digest}),
    ]
    bad_token = [
        client.get(integrity_url, headers={"Authorization": "Bearer not-a-token"}),
        client.post(
            verify_url,
            json={"sha256": digest},
            headers={"Authorization": "Bearer"},
        ),
    ]

    for response in (*unauthenticated, *bad_token):
        assert response.status_code == 401
        body = response.json()
        assert set(body) == PROBLEM_DETAIL_KEYS
        assert body["type"] == "about:blank"
        assert body["title"] == "Unauthorized"
        assert body["status"] == 401
        assert body["detail"] == "Unauthorized"

    missing_integrity = client.get(
        INTEGRITY_URL.format(asset_id=UNKNOWN_ASSET_ID), headers=_auth(token)
    )
    missing_verify = client.post(
        VERIFY_URL.format(asset_id=UNKNOWN_ASSET_ID),
        json={"sha256": digest},
        headers=_auth(token),
    )

    for response, url in (
        (missing_integrity, INTEGRITY_URL),
        (missing_verify, VERIFY_URL),
    ):
        assert response.status_code == 404
        body = response.json()
        assert set(body) == PROBLEM_DETAIL_KEYS
        assert body["title"] == "Not Found"
        assert body["status"] == 404
        assert body["detail"] == f"Media asset not found: {UNKNOWN_ASSET_ID}"
        assert body["instance"] == url.format(asset_id=UNKNOWN_ASSET_ID)

    # A refused read never moves the recorded state.
    stored = _asset(asset.id)
    assert stored is not None
    assert stored.verified is True


def test_media_hash_algorithm_is_sha256_by_default_and_from_the_environment(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MEDIA_HASH_ALGORITHM", raising=False)

    assert MediaSettings.from_env().hash_algorithm == "sha256"

    monkeypatch.setenv("MEDIA_HASH_ALGORITHM", "sha256")

    assert MediaSettings.from_env().hash_algorithm == "sha256"

    for unsupported in ("md5", "SHA-512", "   "):
        monkeypatch.setenv("MEDIA_HASH_ALGORITHM", unsupported)

        # An unsupported or blank name falls back instead of raising.
        assert MediaSettings.from_env().hash_algorithm == "sha256"


S3_ENV_NAMES = (
    "S3_BUCKET",
    "S3_REGION",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_MEDIA_PREFIX",
    "S3_SSE",
)


def test_s3_settings_are_read_from_the_environment_and_fall_back(monkeypatch) -> None:
    for name in S3_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    unset = MediaSettings.from_env()

    assert unset.s3_bucket == ""
    assert unset.s3_region == ""
    assert unset.s3_endpoint_url == ""
    assert unset.s3_access_key_id == ""
    assert unset.s3_secret_access_key == ""
    assert unset.s3_media_prefix == "media"
    assert unset.s3_sse == ""

    monkeypatch.setenv("S3_BUCKET", "bms-media")
    monkeypatch.setenv("S3_REGION", "ap-south-1")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "media-key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "media-secret")
    monkeypatch.setenv("S3_MEDIA_PREFIX", "/uploads/")
    monkeypatch.setenv("S3_SSE", "AES256")

    configured = MediaSettings.from_env()

    assert configured.s3_bucket == "bms-media"
    assert configured.s3_region == "ap-south-1"
    assert configured.s3_endpoint_url == "http://localhost:4566"
    assert configured.s3_access_key_id == "media-key"
    assert configured.s3_secret_access_key == "media-secret"
    assert configured.s3_media_prefix == "/uploads/"
    assert configured.s3_sse == "AES256"

    # A blank value selects the default rather than raising, exactly as the other knobs do.
    for name in S3_ENV_NAMES:
        monkeypatch.setenv(name, "   ")

    blank = MediaSettings.from_env()

    assert blank.s3_bucket == ""
    assert blank.s3_media_prefix == "media"
    assert blank.s3_sse == ""

    # Only AES256 turns server-side encryption on; anything else means none at all.
    for unsupported in ("aws:kms", "none", "AES-256"):
        monkeypatch.setenv("S3_SSE", unsupported)

        assert MediaSettings.from_env().s3_sse == ""
