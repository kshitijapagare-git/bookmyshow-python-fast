"""Presigning a direct-to-storage upload: the pending row, the signed url and the refusals.

The token itself and the exact-size save are exercised without HTTP in
tests/test_presign.py, so this file covers the routes: what `POST /api/media/presign`
creates, what the signed `PUT .../upload` stores and refuses, what
`POST /api/media/{assetId}/complete` links, and that a presigned asset whose bytes never
landed is neither served nor completable. The ratings service is faked through
`get_reviews_client`, exactly as tests/test_review_photo_integrity.py fakes it, because
resolving `ownerId` for a review photo needs a single review the caller is checked
against.
"""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.media import max_bytes_for
from app.models import MediaAsset, User
from app.presign import sign_upload_token
from app.reviews_client import get_reviews_client
from app.routers.media import (
    COMPLETE_FORBIDDEN_DETAIL,
    COMPLETE_MISMATCH_DETAIL,
    INVALID_UPLOAD_TOKEN_DETAIL,
    UPLOAD_MISSING_DETAIL,
    UPLOAD_SIZE_MISMATCH_DETAIL,
    UPLOAD_TYPE_MISMATCH_DETAIL,
)
from app.routers.users import AVATAR_NOT_FOUND_DETAIL
from app.schemas import MediaKind
from app.security import issue_token

PRESIGN_URL = "/api/media/presign"
UPLOAD_URL = "/api/media/{asset_id}/upload"
COMPLETE_URL = "/api/media/{asset_id}/complete"
DOWNLOAD_URL = "/api/media/{asset_id}"
METADATA_URL = "/api/media/{asset_id}/metadata"
AVATAR_URL = "/api/users/me/avatar"
REVIEWS_URL = "/api/movies/{movie_id}/reviews"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
PRESIGN_KEYS = {"assetId", "uploadUrl", "method", "expiresIn", "headers"}
ASSET_KEYS = {"assetId", "kind", "url", "size", "contentType", "sha256"}
MOVIE_ID = 1
REVIEW_ID = 501
FOREIGN_REVIEW_ID = 502
UNKNOWN_REVIEW_ID = 999999
AVATAR_PAYLOAD = b"\x89PNG\r\n\x1a\navatar-bytes"
SECOND_AVATAR_PAYLOAD = b"\x89PNG\r\n\x1a\nother-avatar-bytes"
TAMPERED_AVATAR_PAYLOAD = b"\x89PNG\r\n\x1a\ntampered-after-upload"
PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\nreview-photo-bytes"
SECOND_PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\nother-review-photo-bytes"


def _create_user(phone: str) -> tuple[int, str]:
    """Insert a signed-in moviegoer and mint their JWT, as the review tests do."""
    with SessionLocal() as db:
        user = User(phone=phone, name=None, created_at=datetime.now(timezone.utc))
        db.add(user)
        db.commit()
        return user.id, issue_token(user_id=user.id)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _avatar_body(
    size: int = len(AVATAR_PAYLOAD), content_type: str = "image/png"
) -> dict[str, object]:
    """The avatar declaration a client sends before uploading its image."""
    return {
        "purpose": "avatar",
        "filename": "me.png",
        "contentType": content_type,
        "size": size,
    }


def _review(
    review_id: int, user_id: int, movie_id: int = MOVIE_ID
) -> dict[str, object]:
    """One review, in the shape the ratings service answers a single read with."""
    return {
        "id": review_id,
        "movieId": movie_id,
        "userId": user_id,
        "bookingId": None,
        "groupId": None,
        "rating": 5,
        "comment": "Loved it",
        "verified": True,
        "status": "ACTIVE",
        "createdAt": "2026-10-20T10:00:00+05:30",
    }


class _FakeReviewsService:
    """Stand-in for ReviewsClient: answers one review per id and records each read.

    It answers the public card's page too, so the photo a completion links is read back
    through the same fake the presign ownership check goes through.
    """

    def __init__(self, reviews: dict[int, dict[str, object]] | None = None) -> None:
        self.reviews = reviews if reviews is not None else {}
        self.lookups: list[int] = []

    def get_review(self, *, review_id: int) -> dict[str, object] | None:
        self.lookups.append(review_id)
        return self.reviews.get(review_id)

    def list_reviews(
        self, *, movie_id: int, page: int, page_size: int
    ) -> dict[str, object]:
        rows = [
            review
            for review in self.reviews.values()
            if review.get("movieId") == movie_id
        ]
        return {
            "summary": {
                "average": 4.5,
                "count": len(rows),
                "distribution": {"5": len(rows)},
            },
            "reviews": rows,
            "page": page,
            "pageSize": page_size,
            "total": len(rows),
        }


@contextmanager
def _use_reviews(fake: _FakeReviewsService) -> Iterator[_FakeReviewsService]:
    """Serve the single-review read from the fake; the override is always popped."""
    app.dependency_overrides[get_reviews_client] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_reviews_client, None)


def _presign(client, token: str | None, **body: object):
    headers = {} if token is None else _auth(token)
    return client.post(PRESIGN_URL, json=body, headers=headers)


def _asset(asset_id: int) -> MediaAsset | None:
    """The row itself, so an assertion can check what presign persisted."""
    with SessionLocal() as db:
        return db.get(MediaAsset, asset_id)


def _assets() -> list[MediaAsset]:
    with SessionLocal() as db:
        return list(db.scalars(select(MediaAsset).order_by(MediaAsset.id)))


def _stored_files() -> set[Path]:
    """Every file under the upload directory, .part leftovers included."""
    root = Path(app.state.media_settings.upload_dir)
    return {path for path in root.rglob("*") if path.is_file()}


def _upload(
    client, upload_url: str, payload: bytes, content_type: str = "image/png"
):
    """The browser's one plain PUT of the declared bytes, no session headers at all."""
    return client.put(
        upload_url,
        content=payload,
        headers={"Content-Type": content_type},
    )


def _assert_refused(
    response, status_code: int, title: str, detail: str, asset_id: int
) -> None:
    """One refusal reads exactly as the ProblemDetail contract says it does."""
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == title
    assert body["status"] == status_code
    assert body["detail"] == detail
    assert body["instance"] == UPLOAD_URL.format(asset_id=asset_id)


def _pending_avatar(client, phone: str) -> tuple[int, dict[str, object]]:
    """Presign one avatar for a fresh user; returns their id and the presign body."""
    user_id, token = _create_user(phone)
    created = _presign(client, token, **_avatar_body())
    assert created.status_code == 201
    return user_id, created.json()


def _presigned(client, phone: str) -> tuple[int, str]:
    """A fresh pending avatar: its assetId and the signed url its bytes are PUT to."""
    _, presigned = _pending_avatar(client, phone)
    asset_id = presigned["assetId"]
    assert isinstance(asset_id, int)
    upload_url = presigned["uploadUrl"]
    assert isinstance(upload_url, str)
    return asset_id, upload_url


def _complete(client, asset_id: int, token: str | None = None):
    """The completion call; the bearer token is left off entirely when it is None."""
    headers = {} if token is None else _auth(token)
    return client.post(COMPLETE_URL.format(asset_id=asset_id), headers=headers)


def _upload_avatar(client, token: str, payload: bytes = AVATAR_PAYLOAD) -> int:
    """Presign and upload one avatar for this caller, declaring the payload's size."""
    created = _presign(client, token, **_avatar_body(size=len(payload)))
    assert created.status_code == 201
    body = created.json()
    uploaded = _upload(client, body["uploadUrl"], payload)
    assert uploaded.status_code == 200
    return int(body["assetId"])


def _upload_review_photo(client, token: str, review_id: int, payload: bytes) -> int:
    """Presign and upload one review photo against a review the caller wrote."""
    created = _presign(
        client,
        token,
        purpose="review_photo",
        ownerId=review_id,
        filename="photo.png",
        contentType="image/png",
        size=len(payload),
    )
    assert created.status_code == 201
    body = created.json()
    uploaded = _upload(client, body["uploadUrl"], payload)
    assert uploaded.status_code == 200
    return int(body["assetId"])


def test_avatar_presign_creates_a_pending_avatar_asset(client) -> None:
    user_id, token = _create_user("+919000005001")

    response = _presign(client, token, **_avatar_body())

    assert response.status_code == 201
    body = response.json()
    assert set(body) == PRESIGN_KEYS
    assert body["method"] == "PUT"
    assert body["expiresIn"] == app.state.media_settings.presign_expires_seconds
    assert body["headers"] == {"Content-Type": "image/png"}
    assert body["uploadUrl"].startswith(
        f"/api/media/{body['assetId']}/upload?token="
    )

    asset = _asset(body["assetId"])
    assert asset is not None
    assert asset.kind == MediaKind.AVATAR.value
    assert asset.user_id == user_id
    assert asset.movie_id is None
    assert asset.review_id is None
    assert asset.content_type == "image/png"
    assert asset.size_bytes == len(AVATAR_PAYLOAD)
    assert asset.original_filename == "me.png"
    assert asset.storage_key == ""
    assert asset.url == ""
    assert asset.is_current is False
    assert asset.verified is False
    assert asset.uploaded_at is None


def test_review_photo_presign_uses_the_review_movie_and_owner(client) -> None:
    user_id, token = _create_user("+919000005002")
    fake = _FakeReviewsService({REVIEW_ID: _review(REVIEW_ID, user_id)})

    with _use_reviews(fake):
        response = _presign(
            client,
            token,
            purpose="review_photo",
            ownerId=REVIEW_ID,
            filename="photo.png",
            contentType="image/png",
            size=len(PHOTO_PAYLOAD),
        )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == PRESIGN_KEYS
    assert body["method"] == "PUT"
    assert body["headers"] == {"Content-Type": "image/png"}
    assert body["uploadUrl"].startswith(
        f"/api/media/{body['assetId']}/upload?token="
    )
    # The ownership check is a single read of the review the caller named.
    assert fake.lookups == [REVIEW_ID]

    asset = _asset(body["assetId"])
    assert asset is not None
    assert asset.kind == MediaKind.REVIEW_PHOTO.value
    assert asset.review_id == REVIEW_ID
    assert asset.movie_id == MOVIE_ID
    assert asset.user_id == user_id
    assert asset.size_bytes == len(PHOTO_PAYLOAD)
    assert asset.original_filename == "photo.png"
    assert asset.is_current is False
    assert asset.verified is False
    assert asset.uploaded_at is None


def test_review_photo_presign_for_someone_elses_review_is_403(client) -> None:
    author_id, _ = _create_user("+919000005003")
    _, token = _create_user("+919000005004")
    fake = _FakeReviewsService(
        {FOREIGN_REVIEW_ID: _review(FOREIGN_REVIEW_ID, author_id)}
    )

    with _use_reviews(fake):
        foreign = _presign(
            client,
            token,
            purpose="review_photo",
            ownerId=FOREIGN_REVIEW_ID,
            filename="photo.png",
            contentType="image/png",
            size=len(PHOTO_PAYLOAD),
        )
        unknown = _presign(
            client,
            token,
            purpose="review_photo",
            ownerId=UNKNOWN_REVIEW_ID,
            filename="photo.png",
            contentType="image/png",
            size=len(PHOTO_PAYLOAD),
        )

    for response in (foreign, unknown):
        assert response.status_code == 403
        body = response.json()
        assert set(body) == PROBLEM_DETAIL_KEYS
        assert body["type"] == "about:blank"
        assert body["title"] == "Forbidden"
        assert body["status"] == 403
        # An unknown review and a foreign one read identically, so neither leaks.
        assert body["detail"] == (
            "Only the review's author can upload a photo for this review"
        )
        assert body["instance"] == PRESIGN_URL

    assert fake.lookups == [FOREIGN_REVIEW_ID, UNKNOWN_REVIEW_ID]
    assert _assets() == []


def test_presign_rejects_bad_type_and_oversize_and_missing_owner(client) -> None:
    user_id, token = _create_user("+919000005005")
    image_limit = max_bytes_for(MediaKind.AVATAR, app.state.media_settings)

    with _use_reviews(_FakeReviewsService({REVIEW_ID: _review(REVIEW_ID, user_id)})):
        bad_type = _presign(client, token, **_avatar_body(content_type="image/gif"))
        over_the_limit = _presign(
            client, token, **_avatar_body(size=image_limit + 1)
        )
        no_owner = _presign(
            client,
            token,
            purpose="review_photo",
            filename="photo.png",
            contentType="image/png",
            size=len(PHOTO_PAYLOAD),
        )

    for response in (bad_type, over_the_limit, no_owner):
        assert response.status_code == 422
        body = response.json()
        assert set(body) == PROBLEM_DETAIL_KEYS
        assert body["type"] == "about:blank"
        assert body["status"] == 422
        assert body["instance"] == PRESIGN_URL

    assert _assets() == []


def test_presigned_but_never_uploaded_asset_is_not_served(client) -> None:
    _, token = _create_user("+919000005006")
    created = _presign(client, token, **_avatar_body())
    assert created.status_code == 201
    asset_id = created.json()["assetId"]

    download = client.get(DOWNLOAD_URL.format(asset_id=asset_id))

    assert download.status_code == 404
    body = download.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == f"Media asset not found: {asset_id}"
    assert body["instance"] == DOWNLOAD_URL.format(asset_id=asset_id)

    # The declaration is still readable, which is what the client polls while uploading.
    metadata = client.get(METADATA_URL.format(asset_id=asset_id))

    assert metadata.status_code == 200
    meta = metadata.json()
    assert meta["assetId"] == asset_id
    assert meta["kind"] == MediaKind.AVATAR.value
    assert meta["size"] == len(AVATAR_PAYLOAD)
    assert meta["contentType"] == "image/png"


def test_presign_requires_a_token(client) -> None:
    response = _presign(client, None, **_avatar_body())

    assert response.status_code == 401
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Unauthorized"
    assert body["status"] == 401
    assert body["detail"] == "Unauthorized"
    assert body["instance"] == PRESIGN_URL
    assert _assets() == []


def test_upload_stores_bytes_and_leaves_the_row_pending(client) -> None:
    user_id, presigned = _pending_avatar(client, "+919000005007")
    asset_id = presigned["assetId"]
    assert isinstance(asset_id, int)
    upload_url = presigned["uploadUrl"]
    assert isinstance(upload_url, str)

    uploaded = _upload(client, upload_url, AVATAR_PAYLOAD)

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert set(body) == {"assetId", "kind", "url", "size", "contentType", "sha256"}
    assert body["assetId"] == asset_id
    assert body["kind"] == MediaKind.AVATAR.value
    assert body["size"] == len(AVATAR_PAYLOAD)
    assert body["contentType"] == "image/png"
    assert body["sha256"] == hashlib.sha256(AVATAR_PAYLOAD).hexdigest()
    assert body["url"].startswith("/media/avatars/")
    # The direct upload is plain JSON: it carries no encryption session and no envelope.
    assert uploaded.headers["content-type"] == "application/json"

    asset = _asset(asset_id)
    assert asset is not None
    assert asset.user_id == user_id
    assert asset.kind == MediaKind.AVATAR.value
    assert asset.storage_key == f"avatars/{body['sha256']}.png"
    assert asset.url == body["url"]
    assert asset.content_type == "image/png"
    assert asset.size_bytes == len(AVATAR_PAYLOAD)
    assert asset.sha256 == body["sha256"]
    assert asset.uploaded_at is not None
    # Becoming the owner's current avatar is the completion step's job, not the upload's.
    assert asset.is_current is False
    assert asset.verified is True

    served = client.get(body["url"])

    assert served.status_code == 200
    assert served.content == AVATAR_PAYLOAD


def test_upload_rejects_short_and_long_bodies_with_422_and_stores_nothing(
    client,
) -> None:
    asset_id, upload_url = _presigned(client, "+919000005008")
    before = _stored_files()

    short = _upload(client, upload_url, AVATAR_PAYLOAD[:-1])
    long = _upload(client, upload_url, AVATAR_PAYLOAD + b"x")

    for response in (short, long):
        _assert_refused(
            response,
            422,
            "Unprocessable Entity",
            UPLOAD_SIZE_MISMATCH_DETAIL,
            asset_id,
        )

    assert _stored_files() == before
    asset = _asset(asset_id)
    assert asset is not None
    assert asset.storage_key == ""
    assert asset.uploaded_at is None
    assert asset.is_current is False


def test_upload_rejects_expired_forged_and_foreign_tokens_with_403(client) -> None:
    asset_id, upload_url = _presigned(client, "+919000005009")
    settings = app.state.media_settings
    before = _stored_files()

    expired = sign_upload_token(
        settings,
        asset_id=asset_id,
        kind=MediaKind.AVATAR,
        size=len(AVATAR_PAYLOAD),
        content_type="image/png",
        expires_in_seconds=-60,
    )
    good = upload_url.split("token=", 1)[1]
    header, claims, signature = good.split(".")
    rewritten = ("a" if signature[0] != "a" else "b") + signature[1:]
    forged = f"{header}.{claims}.{rewritten}"

    missing = _upload(client, UPLOAD_URL.format(asset_id=asset_id), AVATAR_PAYLOAD)
    stale = _upload(
        client, f"{UPLOAD_URL.format(asset_id=asset_id)}?token={expired}", AVATAR_PAYLOAD
    )
    tampered = _upload(
        client, f"{UPLOAD_URL.format(asset_id=asset_id)}?token={forged}", AVATAR_PAYLOAD
    )
    # The same, valid token replayed against another asset's upload path.
    moved = _upload(
        client,
        f"{UPLOAD_URL.format(asset_id=asset_id + 1)}?token={good}",
        AVATAR_PAYLOAD,
    )

    for response in (missing, stale, tampered):
        _assert_refused(
            response, 403, "Forbidden", INVALID_UPLOAD_TOKEN_DETAIL, asset_id
        )

    _assert_refused(
        moved, 403, "Forbidden", INVALID_UPLOAD_TOKEN_DETAIL, asset_id + 1
    )

    assert _stored_files() == before
    asset = _asset(asset_id)
    assert asset is not None
    assert asset.storage_key == ""
    assert asset.uploaded_at is None


def test_upload_rejects_a_content_type_mismatch_with_422(client) -> None:
    asset_id, upload_url = _presigned(client, "+919000005010")
    before = _stored_files()

    response = _upload(client, upload_url, AVATAR_PAYLOAD, "text/plain")

    _assert_refused(
        response, 422, "Unprocessable Entity", UPLOAD_TYPE_MISMATCH_DETAIL, asset_id
    )
    assert _stored_files() == before
    asset = _asset(asset_id)
    assert asset is not None
    assert asset.storage_key == ""
    assert asset.uploaded_at is None
    # The declaration made at presign is untouched by the refused upload.
    assert asset.content_type == "image/png"
    assert asset.size_bytes == len(AVATAR_PAYLOAD)


def test_avatar_presign_upload_complete_end_to_end(client) -> None:
    user_id, token = _create_user("+919000005011")
    asset_id = _upload_avatar(client, token)

    completed = _complete(client, asset_id, token)

    assert completed.status_code == 200
    body = completed.json()
    assert set(body) == ASSET_KEYS
    assert body["assetId"] == asset_id
    assert body["kind"] == MediaKind.AVATAR.value
    assert body["url"].startswith("/media/avatars/")
    assert body["size"] == len(AVATAR_PAYLOAD)
    assert body["contentType"] == "image/png"
    assert body["sha256"] == hashlib.sha256(AVATAR_PAYLOAD).hexdigest()

    asset = _asset(asset_id)
    assert asset is not None
    assert asset.user_id == user_id
    assert asset.is_current is True
    assert asset.verified is True
    assert asset.uploaded_at is not None

    # The completion's own url, and the asset route, both serve the uploaded bytes.
    assert client.get(body["url"]).content == AVATAR_PAYLOAD
    served = client.get(DOWNLOAD_URL.format(asset_id=asset_id))

    assert served.status_code == 200
    assert served.content == AVATAR_PAYLOAD


def test_complete_demotes_the_previous_asset_of_that_kind(client) -> None:
    user_id, token = _create_user("+919000005012")
    first_id = _upload_avatar(client, token, AVATAR_PAYLOAD)
    second_id = _upload_avatar(client, token, SECOND_AVATAR_PAYLOAD)

    assert _complete(client, first_id, token).status_code == 200
    assert _complete(client, second_id, token).status_code == 200

    first = _asset(first_id)
    assert first is not None
    assert first.is_current is False
    # The superseded avatar keeps its row and its file, exactly as a poster does.
    assert (Path(app.state.media_settings.upload_dir) / first.storage_key).is_file()

    second = _asset(second_id)
    assert second is not None
    assert second.user_id == user_id
    assert second.is_current is True

    fake = _FakeReviewsService({REVIEW_ID: _review(REVIEW_ID, user_id)})
    with _use_reviews(fake):
        photo_first = _upload_review_photo(client, token, REVIEW_ID, PHOTO_PAYLOAD)
        photo_second = _upload_review_photo(
            client, token, REVIEW_ID, SECOND_PHOTO_PAYLOAD
        )

    assert _complete(client, photo_first, token).status_code == 200
    assert _complete(client, photo_second, token).status_code == 200

    old_photo = _asset(photo_first)
    assert old_photo is not None
    assert old_photo.review_id == REVIEW_ID
    assert old_photo.is_current is False

    new_photo = _asset(photo_second)
    assert new_photo is not None
    assert new_photo.review_id == REVIEW_ID
    assert new_photo.is_current is True

    # Completing a review photo leaves the same moviegoer's avatar alone.
    avatar = _asset(second_id)
    assert avatar is not None
    assert avatar.is_current is True


def test_complete_for_another_user_is_403(client) -> None:
    _, owner_token = _create_user("+919000005013")
    asset_id = _upload_avatar(client, owner_token)
    _, stranger_token = _create_user("+919000005014")

    response = _complete(client, asset_id, stranger_token)

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == COMPLETE_FORBIDDEN_DETAIL
    assert body["instance"] == COMPLETE_URL.format(asset_id=asset_id)

    # Authentication is not authorization: the refused call changes nothing.
    asset = _asset(asset_id)
    assert asset is not None
    assert asset.is_current is False
    assert asset.uploaded_at is not None

    assert _complete(client, asset_id, owner_token).status_code == 200


def test_complete_rejects_never_uploaded_and_tampered_bytes(client) -> None:
    _, token = _create_user("+919000005015")
    pending = _presign(client, token, **_avatar_body())
    assert pending.status_code == 201
    pending_id = int(pending.json()["assetId"])

    never_uploaded = _complete(client, pending_id, token)

    assert never_uploaded.status_code == 409
    body = never_uploaded.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Conflict"
    assert body["status"] == 409
    assert body["detail"] == UPLOAD_MISSING_DETAIL
    assert body["instance"] == COMPLETE_URL.format(asset_id=pending_id)

    pending_asset = _asset(pending_id)
    assert pending_asset is not None
    assert pending_asset.is_current is False

    tampered_id = _upload_avatar(client, token, AVATAR_PAYLOAD)
    tampered = _asset(tampered_id)
    assert tampered is not None
    path = Path(app.state.media_settings.upload_dir) / tampered.storage_key
    assert path.is_file()
    path.write_bytes(TAMPERED_AVATAR_PAYLOAD)

    response = _complete(client, tampered_id, token)

    assert response.status_code == 422
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Unprocessable Entity"
    assert body["status"] == 422
    assert body["detail"] == COMPLETE_MISMATCH_DETAIL
    assert body["instance"] == COMPLETE_URL.format(asset_id=tampered_id)

    stored = _asset(tampered_id)
    assert stored is not None
    assert stored.is_current is False


def test_completed_review_photo_is_the_public_review_card_photo(client) -> None:
    user_id, token = _create_user("+919000005016")
    fake = _FakeReviewsService({REVIEW_ID: _review(REVIEW_ID, user_id)})

    with _use_reviews(fake):
        asset_id = _upload_review_photo(client, token, REVIEW_ID, PHOTO_PAYLOAD)
        completed = _complete(client, asset_id, token)
        assert completed.status_code == 200
        page = client.get(REVIEWS_URL.format(movie_id=MOVIE_ID))

    assert page.status_code == 200
    review = page.json()["reviews"][0]
    assert review["id"] == REVIEW_ID
    assert review["photo"] == {
        "url": completed.json()["url"],
        "sha256": hashlib.sha256(PHOTO_PAYLOAD).hexdigest(),
        "verified": True,
    }
    assert client.get(review["photo"]["url"]).content == PHOTO_PAYLOAD


def test_avatar_read_path_restores_the_latest_avatar(client) -> None:
    user_id, token = _create_user("+919000005017")
    first_id = _upload_avatar(client, token, AVATAR_PAYLOAD)
    assert _complete(client, first_id, token).status_code == 200

    first_read = client.get(AVATAR_URL, headers=_auth(token))

    assert first_read.status_code == 200
    body = first_read.json()
    assert set(body) == ASSET_KEYS
    assert body["assetId"] == first_id
    assert body["kind"] == MediaKind.AVATAR.value
    assert body["url"].startswith("/media/avatars/")
    assert body["size"] == len(AVATAR_PAYLOAD)
    assert body["contentType"] == "image/png"
    assert body["sha256"] == hashlib.sha256(AVATAR_PAYLOAD).hexdigest()

    # A fresh request to the url the read path answered still serves the bytes, which
    # is what a reload needs to restore the photo.
    served = client.get(body["url"])

    assert served.status_code == 200
    assert served.content == AVATAR_PAYLOAD

    second_id = _upload_avatar(client, token, SECOND_AVATAR_PAYLOAD)
    assert _complete(client, second_id, token).status_code == 200

    second_read = client.get(AVATAR_URL, headers=_auth(token))

    assert second_read.status_code == 200
    latest = second_read.json()
    assert set(latest) == ASSET_KEYS
    assert latest["assetId"] == second_id
    assert latest["kind"] == MediaKind.AVATAR.value
    assert latest["sha256"] == hashlib.sha256(SECOND_AVATAR_PAYLOAD).hexdigest()

    older = _asset(first_id)
    assert older is not None
    assert older.user_id == user_id
    assert older.is_current is False
    assert client.get(latest["url"]).content == SECOND_AVATAR_PAYLOAD


def test_avatar_read_path_is_404_without_one_and_401_without_a_token(client) -> None:
    _, token = _create_user("+919000005018")

    missing = client.get(AVATAR_URL, headers=_auth(token))

    assert missing.status_code == 404
    body = missing.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == AVATAR_NOT_FOUND_DETAIL
    assert body["instance"] == AVATAR_URL

    anonymous = client.get(AVATAR_URL)

    assert anonymous.status_code == 401
    problem = anonymous.json()
    assert set(problem) == PROBLEM_DETAIL_KEYS
    assert problem["type"] == "about:blank"
    assert problem["title"] == "Unauthorized"
    assert problem["status"] == 401
    assert problem["detail"] == "Unauthorized"
    assert problem["instance"] == AVATAR_URL
