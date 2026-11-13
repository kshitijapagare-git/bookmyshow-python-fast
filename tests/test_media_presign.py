"""The direct-upload flow: presign a row, PUT the bytes, then complete the asset.

Every request goes through the plain `client` fixture, so the module runs with
CRYPTO_ENABLED=false exactly as the rest of the suite does; the encrypted transport is
covered separately in tests/test_crypto.py. The upload itself is the one step a browser
performs without the moviegoer's JWT -- the signed url carries its own token -- so the
tests take the `uploadUrl` presign answered and PUT the bytes there.

The ratings service is faked through the same `get_reviews_client` dependency override
tests/test_review_photo_integrity.py uses, because `purpose=review_photo` resolves its
`ownerId` against the review the caller is supposed to have written. An expired token is
produced by signing one with an expiry in the past rather than by waiting, and the bytes
land in the fresh MEDIA_UPLOAD_DIR tests/conftest.py points at.
"""

import hashlib
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.media import KIND_SUBDIRS, MediaSettings, get_media_settings
from app.media_presign import sign_upload_token
from app.models import MediaAsset, User
from app.reviews_client import ReviewsUnavailableError, get_reviews_client
from app.routers.media import (
    ASSET_FORBIDDEN_DETAIL,
    COMPLETE_NOT_UPLOADED_DETAIL,
    PRESIGN_INVALID_DETAIL,
    REVIEW_NOT_FOUND_DETAIL,
    REVIEW_PHOTO_FORBIDDEN_DETAIL,
    UPLOAD_TOKEN_INVALID_DETAIL,
)
from app.routers.reviews import REVIEWS_UNAVAILABLE_DETAIL
from app.schemas import MediaKind
from app.security import issue_token

PRESIGN_URL = "/api/media/presign"
COMPLETE_URL = "/api/media/{assetId}/complete"
UPLOAD_URL = "/api/media/{assetId}/upload"
PRESIGN_KEYS = {"assetId", "uploadUrl", "method", "expiresIn", "headers"}
COMPLETE_KEYS = {
    "assetId",
    "url",
    "contentType",
    "size",
    "sha256",
    "kind",
    "verified",
}
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\navatar-bytes"
SECOND_PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\nsecond-avatar-bytes"
REVIEW_PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\nreview-photo-bytes"
MOVIE_ID = 1
REVIEW_ID = 501
UNKNOWN_ASSET_ID = 999999
DEFAULT_PRESIGN_EXPIRES_SECONDS = 300


def _create_user(phone: str) -> tuple[int, str]:
    """Insert a signed-in moviegoer and mint their JWT, as the existing media tests do."""
    with SessionLocal() as db:
        user = User(phone=phone, name=None, created_at=datetime.now(timezone.utc))
        db.add(user)
        db.commit()
        return user.id, issue_token(user_id=user.id)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _presign(
    client,
    token: str,
    *,
    purpose: str = "avatar",
    owner_id: int | None = None,
    content_type: str = "image/png",
    size: int = len(PHOTO_PAYLOAD),
    filename: str = "me.png",
) -> httpx.Response:
    """One presign call; ownerId is left off entirely when it is None."""
    body: dict[str, object] = {
        "purpose": purpose,
        "filename": filename,
        "contentType": content_type,
        "size": size,
    }
    if owner_id is not None:
        body["ownerId"] = owner_id
    return client.post(PRESIGN_URL, json=body, headers=_auth(token))


def _upload_bytes(
    client,
    upload_url: str,
    payload: bytes,
    content_type: str = "image/png",
) -> httpx.Response:
    """PUT the bytes at the presigned url, declaring the content type it was signed for."""
    return client.put(upload_url, content=payload, headers={"Content-Type": content_type})


def _complete(client, asset_id: int, token: str) -> httpx.Response:
    return client.post(COMPLETE_URL.format(assetId=asset_id), headers=_auth(token))


def _asset(asset_id: int) -> MediaAsset | None:
    """The row itself, so an assertion can read the persisted flags."""
    with SessionLocal() as db:
        return db.get(MediaAsset, asset_id)


def _assets(kind: str | None = None, user_id: int | None = None) -> list[MediaAsset]:
    """Every MediaAsset row, optionally narrowed to one kind and one owner."""
    with SessionLocal() as db:
        query = select(MediaAsset).order_by(MediaAsset.id)
        if kind is not None:
            query = query.where(MediaAsset.kind == kind)
        if user_id is not None:
            query = query.where(MediaAsset.user_id == user_id)
        return list(db.scalars(query))


def _stored_files() -> set[Path]:
    """Every file under the upload directory, .part leftovers included."""
    root = Path(app.state.media_settings.upload_dir)
    return {path for path in root.rglob("*") if path.is_file()}


def _settings(**overrides: object) -> MediaSettings:
    """The app's own settings with a few knobs replaced, the presign ones included."""
    return replace(app.state.media_settings, **overrides)


@contextmanager
def _use_media(settings: MediaSettings) -> Iterator[MediaSettings]:
    """Serve these settings; the override is always popped again."""
    app.dependency_overrides[get_media_settings] = lambda: settings
    try:
        yield settings
    finally:
        app.dependency_overrides.pop(get_media_settings, None)


def _review(review_id: int, user_id: int, movie_id: int = MOVIE_ID) -> dict[str, object]:
    """One review as the ratings API answers it, with the author's userId."""
    return {
        "id": review_id,
        "movieId": movie_id,
        "userId": user_id,
        "bookingId": 77,
        "groupId": None,
        "rating": 5,
        "comment": "Loved it",
        "verified": True,
        "status": "ACTIVE",
        "createdAt": "2026-10-20T10:00:00+05:30",
    }


class _FakeReviewsService:
    """Stand-in for ReviewsClient: answers one review by id and one page of reviews."""

    def __init__(
        self, review: dict[str, object] | None = None, outcome: str = "succeed"
    ) -> None:
        self.review = review
        self.outcome = outcome
        self.lookups: list[int] = []
        self.pages: list[dict[str, int]] = []

    def get_review(self, *, review_id: int) -> dict[str, object]:
        self.lookups.append(review_id)
        if self.outcome == "unavailable":
            raise ReviewsUnavailableError("reviews service answered 500")
        if self.review is None or self.review["id"] != review_id:
            return {}
        return dict(self.review)

    def list_reviews(
        self, *, movie_id: int, page: int, page_size: int
    ) -> dict[str, object]:
        self.pages.append({"movie_id": movie_id, "page": page, "page_size": page_size})
        if self.outcome == "unavailable":
            raise ReviewsUnavailableError("reviews service answered 500")
        reviews = [dict(self.review)] if self.review is not None else []
        return {
            "summary": {
                "average": 4.5,
                "count": len(reviews),
                "distribution": {"5": 1} if reviews else {},
            },
            "reviews": reviews,
            "page": page,
            "pageSize": page_size,
            "total": len(reviews),
        }


@contextmanager
def _use_reviews(fake: _FakeReviewsService) -> Iterator[_FakeReviewsService]:
    """Serve the ratings reads from the fake; the override is always popped again."""
    app.dependency_overrides[get_reviews_client] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_reviews_client, None)


def _assert_problem(response: httpx.Response, *, status_code: int, instance: str) -> dict:
    """Assert the ticket's status code and the shared ProblemDetail body."""
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["status"] == status_code
    assert body["detail"]
    assert body["instance"] == instance
    return body


def test_avatar_presign_upload_and_complete_stores_the_profile_photo(client) -> None:
    user_id, token = _create_user("+919000005001")

    presigned = _presign(client, token)

    assert presigned.status_code == 200
    body = presigned.json()
    assert set(body) == PRESIGN_KEYS
    assert body["method"] == "PUT"
    assert body["headers"] == {"Content-Type": "image/png"}
    assert body["expiresIn"] == app.state.media_settings.presign_expires_seconds
    assert body["uploadUrl"].startswith(f"/api/media/{body['assetId']}/upload?token=")

    pending = _asset(body["assetId"])
    assert pending is not None
    assert pending.kind == MediaKind.AVATAR.value
    assert pending.user_id == user_id
    assert pending.movie_id is None
    assert pending.review_id is None
    assert pending.content_type == "image/png"
    assert pending.storage_key is None
    assert pending.url is None
    assert pending.sha256 is None
    assert pending.size_bytes == len(PHOTO_PAYLOAD)
    assert pending.verified is False
    assert pending.is_current is False

    uploaded = _upload_bytes(client, body["uploadUrl"], PHOTO_PAYLOAD)

    assert uploaded.status_code == 204
    assert uploaded.content == b""

    landed = _asset(body["assetId"])
    assert landed is not None
    assert landed.storage_key.startswith(f"{KIND_SUBDIRS[MediaKind.AVATAR]}/")
    assert landed.url.startswith("/media/avatars/")
    assert landed.sha256 == hashlib.sha256(PHOTO_PAYLOAD).hexdigest()
    assert landed.size_bytes == len(PHOTO_PAYLOAD)
    assert (
        Path(app.state.media_settings.upload_dir) / landed.storage_key
    ).read_bytes() == PHOTO_PAYLOAD

    completed = _complete(client, body["assetId"], token)

    assert completed.status_code == 200
    complete_body = completed.json()
    assert set(complete_body) == COMPLETE_KEYS
    assert complete_body == {
        "assetId": body["assetId"],
        "url": landed.url,
        "contentType": "image/png",
        "size": len(PHOTO_PAYLOAD),
        "sha256": hashlib.sha256(PHOTO_PAYLOAD).hexdigest(),
        "kind": "AVATAR",
        "verified": True,
    }

    current = _asset(body["assetId"])
    assert current is not None
    assert current.verified is True
    assert current.is_current is True

    # The url complete answered really serves the bytes the upload landed.
    served = client.get(complete_body["url"])
    assert served.status_code == 200
    assert served.content == PHOTO_PAYLOAD


def test_a_second_avatar_supersedes_the_first_one(client) -> None:
    user_id, token = _create_user("+919000005002")

    first = _presign(client, token).json()
    assert _upload_bytes(client, first["uploadUrl"], PHOTO_PAYLOAD).status_code == 204
    assert _complete(client, first["assetId"], token).status_code == 200

    second = _presign(client, token, size=len(SECOND_PHOTO_PAYLOAD)).json()
    assert (
        _upload_bytes(client, second["uploadUrl"], SECOND_PHOTO_PAYLOAD).status_code
        == 204
    )
    assert _complete(client, second["assetId"], token).status_code == 200

    rows = _assets(MediaKind.AVATAR.value, user_id)

    assert [row.id for row in rows] == [first["assetId"], second["assetId"]]
    assert [row.is_current for row in rows] == [False, True]
    # Exactly one current avatar, and both photos keep their own bytes.
    assert len([row for row in rows if row.is_current]) == 1
    assert client.get(rows[0].url).content == PHOTO_PAYLOAD
    assert client.get(rows[1].url).content == SECOND_PHOTO_PAYLOAD


def test_review_photo_presign_upload_and_complete_links_the_review(client) -> None:
    user_id, token = _create_user("+919000005003")
    fake = _FakeReviewsService(review=_review(REVIEW_ID, user_id))

    with _use_reviews(fake):
        presigned = _presign(
            client,
            token,
            purpose="review_photo",
            owner_id=REVIEW_ID,
            size=len(REVIEW_PHOTO_PAYLOAD),
            filename="review.png",
        )

    assert presigned.status_code == 200
    body = presigned.json()
    assert set(body) == PRESIGN_KEYS
    assert fake.lookups == [REVIEW_ID]

    pending = _asset(body["assetId"])
    assert pending is not None
    assert pending.kind == MediaKind.REVIEW_PHOTO.value
    assert pending.review_id == REVIEW_ID
    assert pending.user_id == user_id
    assert pending.movie_id is None
    assert pending.storage_key is None
    assert pending.verified is False
    assert pending.is_current is False

    uploaded = _upload_bytes(client, body["uploadUrl"], REVIEW_PHOTO_PAYLOAD)
    assert uploaded.status_code == 204

    completed = _complete(client, body["assetId"], token)

    assert completed.status_code == 200
    complete_body = completed.json()
    assert set(complete_body) == COMPLETE_KEYS
    assert complete_body["kind"] == "REVIEW_PHOTO"
    assert complete_body["verified"] is True
    assert complete_body["url"].startswith("/media/review-photos/")
    assert complete_body["sha256"] == hashlib.sha256(REVIEW_PHOTO_PAYLOAD).hexdigest()
    assert complete_body["size"] == len(REVIEW_PHOTO_PAYLOAD)

    stored = _asset(body["assetId"])
    assert stored is not None
    assert stored.review_id == REVIEW_ID
    assert stored.is_current is True
    assert stored.verified is True

    # The public card now shows the photo, because the row is current and trusted.
    with _use_reviews(_FakeReviewsService(review=_review(REVIEW_ID, user_id))):
        page = client.get(f"/api/movies/{MOVIE_ID}/reviews")

    assert page.status_code == 200
    listed = page.json()["reviews"][0]
    assert listed["id"] == REVIEW_ID
    assert listed["photo"] == {
        "url": complete_body["url"],
        "sha256": hashlib.sha256(REVIEW_PHOTO_PAYLOAD).hexdigest(),
        "verified": True,
    }


def test_presign_review_photo_without_an_owner_id_is_422(client) -> None:
    _, token = _create_user("+919000005004")

    response = _presign(client, token, purpose="review_photo")

    body = _assert_problem(response, status_code=422, instance=PRESIGN_URL)
    assert body["detail"]
    assert _assets() == []


def test_presign_review_photo_of_another_author_is_403(client) -> None:
    author_id, _ = _create_user("+919000005005")
    _, token = _create_user("+919000005006")
    fake = _FakeReviewsService(review=_review(REVIEW_ID, author_id))

    with _use_reviews(fake):
        response = _presign(
            client, token, purpose="review_photo", owner_id=REVIEW_ID
        )

    body = _assert_problem(response, status_code=403, instance=PRESIGN_URL)
    assert body["title"] == "Forbidden"
    assert body["detail"] == REVIEW_PHOTO_FORBIDDEN_DETAIL
    assert fake.lookups == [REVIEW_ID]
    assert _assets() == []


def test_presign_review_photo_for_an_unknown_review_is_404(client) -> None:
    _, token = _create_user("+919000005007")
    fake = _FakeReviewsService()

    with _use_reviews(fake):
        response = _presign(
            client, token, purpose="review_photo", owner_id=REVIEW_ID
        )

    body = _assert_problem(response, status_code=404, instance=PRESIGN_URL)
    assert body["title"] == "Not Found"
    assert body["detail"] == f"{REVIEW_NOT_FOUND_DETAIL}: {REVIEW_ID}"
    assert _assets() == []


def test_presign_review_photo_when_the_reviews_service_is_down_is_503(client) -> None:
    _, token = _create_user("+919000005008")
    fake = _FakeReviewsService(outcome="unavailable")

    with _use_reviews(fake):
        response = _presign(
            client, token, purpose="review_photo", owner_id=REVIEW_ID
        )

    body = _assert_problem(response, status_code=503, instance=PRESIGN_URL)
    assert body["title"] == "Service Unavailable"
    assert body["detail"] == REVIEWS_UNAVAILABLE_DETAIL
    assert _assets() == []


def test_presign_with_an_unsupported_content_type_is_422(client) -> None:
    _, token = _create_user("+919000005009")

    response = _presign(client, token, content_type="image/gif")

    body = _assert_problem(response, status_code=422, instance=PRESIGN_URL)
    assert body["detail"] == PRESIGN_INVALID_DETAIL
    assert _assets() == []


def test_presign_over_the_image_limit_is_422(client) -> None:
    _, token = _create_user("+919000005010")

    with _use_media(_settings(max_image_bytes=8)):
        at_the_limit = _presign(client, token, size=8)
        over_the_limit = _presign(client, token, size=9)

    assert at_the_limit.status_code == 200

    body = _assert_problem(over_the_limit, status_code=422, instance=PRESIGN_URL)
    assert body["detail"] == PRESIGN_INVALID_DETAIL

    # Only the upload that fits the limit was reserved.
    rows = _assets()
    assert [row.id for row in rows] == [at_the_limit.json()["assetId"]]
    assert rows[0].size_bytes == 8


def test_presign_and_complete_require_a_token(client) -> None:
    user_id, token = _create_user("+919000005011")
    asset_id = _presign(client, token).json()["assetId"]

    presigned = client.post(
        PRESIGN_URL,
        json={
            "purpose": "avatar",
            "filename": "me.png",
            "contentType": "image/png",
            "size": len(PHOTO_PAYLOAD),
        },
    )
    completed = client.post(COMPLETE_URL.format(assetId=asset_id))

    for response, instance in (
        (presigned, PRESIGN_URL),
        (completed, COMPLETE_URL.format(assetId=asset_id)),
    ):
        body = _assert_problem(response, status_code=401, instance=instance)
        assert body["title"] == "Unauthorized"
        assert body["detail"] == "Unauthorized"

    # Only the row the signed-in presign created exists.
    rows = _assets()
    assert [row.id for row in rows] == [asset_id]
    assert rows[0].user_id == user_id


def test_upload_with_an_invalid_or_expired_token_is_403(client) -> None:
    _, token = _create_user("+919000005012")
    secret = app.state.media_settings.presign_secret
    before = _stored_files()

    for kind in ("missing", "altered", "other-asset", "expired"):
        presigned = _presign(client, token)
        assert presigned.status_code == 200
        body = presigned.json()
        asset_id = body["assetId"]
        matchable = UPLOAD_URL.format(assetId=asset_id)

        if kind == "missing":
            upload_url = matchable
        elif kind == "altered":
            expiry, _, signature = body["uploadUrl"].partition("token=")[2].partition(".")
            flipped = "1" if signature[0] != "1" else "0"
            upload_url = f"{matchable}?token={expiry}.{flipped}{signature[1:]}"
        elif kind == "other-asset":
            other = sign_upload_token(
                asset_id=asset_id + 1, expires_at=int(time.time()) + 300, secret=secret
            )
            upload_url = f"{matchable}?token={other}"
        else:
            expired = sign_upload_token(
                asset_id=asset_id, expires_at=int(time.time()) - 1, secret=secret
            )
            upload_url = f"{matchable}?token={expired}"

        response = _upload_bytes(client, upload_url, PHOTO_PAYLOAD)

        problem = _assert_problem(response, status_code=403, instance=matchable)
        assert problem["title"] == "Forbidden"
        assert problem["detail"] == UPLOAD_TOKEN_INVALID_DETAIL

        refused = _asset(asset_id)
        assert refused is not None
        assert refused.storage_key is None

    assert _stored_files() == before


def test_upload_with_a_mismatched_content_type_is_422(client) -> None:
    _, token = _create_user("+919000005013")
    body = _presign(client, token).json()
    matchable = UPLOAD_URL.format(assetId=body["assetId"])
    before = _stored_files()

    response = _upload_bytes(client, body["uploadUrl"], PHOTO_PAYLOAD, "image/gif")

    problem = _assert_problem(response, status_code=422, instance=matchable)
    assert problem["detail"] == PRESIGN_INVALID_DETAIL
    assert _stored_files() == before
    pending = _asset(body["assetId"])
    assert pending is not None
    assert pending.storage_key is None


def test_upload_larger_than_the_presigned_size_is_422(client) -> None:
    _, token = _create_user("+919000005014")
    body = _presign(client, token, size=len(PHOTO_PAYLOAD)).json()
    matchable = UPLOAD_URL.format(assetId=body["assetId"])
    before = _stored_files()

    response = _upload_bytes(client, body["uploadUrl"], PHOTO_PAYLOAD + b"extra")

    problem = _assert_problem(response, status_code=422, instance=matchable)
    assert problem["detail"] == PRESIGN_INVALID_DETAIL
    assert _stored_files() == before
    assert not list(Path(app.state.media_settings.upload_dir).rglob("*.part"))
    pending = _asset(body["assetId"])
    assert pending is not None
    assert pending.storage_key is None


def test_complete_before_the_upload_is_422(client) -> None:
    user_id, token = _create_user("+919000005015")
    body = _presign(client, token).json()
    matchable = COMPLETE_URL.format(assetId=body["assetId"])

    response = _complete(client, body["assetId"], token)

    problem = _assert_problem(response, status_code=422, instance=matchable)
    assert problem["detail"] == COMPLETE_NOT_UPLOADED_DETAIL
    pending = _asset(body["assetId"])
    assert pending is not None
    assert pending.user_id == user_id
    assert pending.verified is False
    assert pending.is_current is False


def test_complete_by_another_user_is_403_and_an_unknown_asset_is_404(client) -> None:
    _, token = _create_user("+919000005016")
    _, stranger_token = _create_user("+919000005017")
    body = _presign(client, token).json()
    assert _upload_bytes(client, body["uploadUrl"], PHOTO_PAYLOAD).status_code == 204

    stranger = _complete(client, body["assetId"], stranger_token)

    problem = _assert_problem(
        stranger,
        status_code=403,
        instance=COMPLETE_URL.format(assetId=body["assetId"]),
    )
    assert problem["detail"] == ASSET_FORBIDDEN_DETAIL

    unknown = _complete(client, UNKNOWN_ASSET_ID, token)

    missing = _assert_problem(
        unknown, status_code=404, instance=COMPLETE_URL.format(assetId=UNKNOWN_ASSET_ID)
    )
    assert missing["title"] == "Not Found"
    assert missing["detail"] == f"Media asset not found: {UNKNOWN_ASSET_ID}"

    # The owner's own complete still succeeds after those two refusals.
    assert _complete(client, body["assetId"], token).status_code == 200


def test_presign_settings_come_from_the_environment_and_reach_the_response(
    client, monkeypatch
) -> None:
    monkeypatch.delenv("MEDIA_PRESIGN_EXPIRES_SECONDS", raising=False)
    monkeypatch.delenv("MEDIA_PRESIGN_SECRET", raising=False)

    assert (
        MediaSettings.from_env().presign_expires_seconds
        == DEFAULT_PRESIGN_EXPIRES_SECONDS
    )

    monkeypatch.setenv("MEDIA_PRESIGN_EXPIRES_SECONDS", "900")
    monkeypatch.setenv("MEDIA_PRESIGN_SECRET", "  s3cret  ")

    settings = MediaSettings.from_env()

    assert settings.presign_expires_seconds == 900
    assert settings.presign_secret == "s3cret"

    _, token = _create_user("+919000005018")
    with _use_media(_settings(presign_expires_seconds=900)) as overridden:
        presigned = _presign(client, token)
        body = presigned.json()
        uploaded = _upload_bytes(client, body["uploadUrl"], PHOTO_PAYLOAD)

    assert presigned.status_code == 200
    assert body["expiresIn"] == overridden.presign_expires_seconds == 900
    # The token it handed out is valid for that long, so the upload still lands.
    assert uploaded.status_code == 204
