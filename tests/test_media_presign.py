"""The presigned upload flow: presign a PUT url, send raw bytes, then complete.

A signed-in moviegoer uploads a profile photo or a review photo straight to storage, so
the bytes never travel inside an encrypted JSON body: `POST /api/media/presign` creates
the pending asset row and answers a signed `PUT` url, that raw `PUT` streams the bytes
into the same `StorageBackend` the multipart routes use, and
`POST /api/media/{assetId}/complete` re-derives the digest and size from what storage
holds before making the asset current for its owner.

The ratings service owns the reviews and exposes no by-id read, so a review's authorship
is resolved from the local `review_authors` index; the tests that need one insert it
directly through SessionLocal, exactly as tests/test_movie_reviews.py inserts the user it
authenticates with. An expired upload token is minted with a past expiry through
`sign_presign_token`, so nothing sleeps and no test state is left behind.
"""

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import MediaAsset, ReviewAuthor, User
from app.presign import sign_presign_token
from app.routers.media import (
    INCOMPLETE_ASSET_DETAIL,
    PRESIGN_FORBIDDEN_DETAIL,
    REVIEW_OWNER_FORBIDDEN_DETAIL,
    SIZE_MISMATCH_DETAIL,
    TYPE_MISMATCH_DETAIL,
)
from app.schemas import MediaKind
from app.security import issue_token

PRESIGN_URL = "/api/media/presign"
COMPLETE_URL = "/api/media/{asset_id}/complete"
DOWNLOAD_URL = "/api/media/{asset_id}"
PRESIGN_KEYS = {"assetId", "uploadUrl", "method", "expiresIn", "headers"}
COMPLETE_KEYS = {"assetId", "url", "sha256", "size", "contentType", "kind", "verified"}
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
UNKNOWN_ASSET_ID = 999999
MOVIE_ID = 1
REVIEW_ID = 501
AVATAR_PAYLOAD = b"\x89PNG\r\n\x1a\navatar-bytes"
SECOND_AVATAR_PAYLOAD = b"\x89PNG\r\n\x1a\nsecond-avatar-bytes"
REVIEW_PHOTO_PAYLOAD = b"\x89PNG\r\n\x1a\nreview-photo-bytes"


def _create_user(phone: str) -> tuple[int, str]:
    """Insert a signed-in moviegoer and mint their JWT, as the review tests do."""
    with SessionLocal() as db:
        user = User(phone=phone, name=None, created_at=datetime.now(timezone.utc))
        db.add(user)
        db.commit()
        return user.id, issue_token(user_id=user.id)


def _record_review(*, review_id: int, user_id: int, movie_id: int) -> None:
    """Record who wrote a review, so `purpose=review_photo` can be authorised locally."""
    with SessionLocal() as db:
        db.add(
            ReviewAuthor(review_id=review_id, user_id=user_id, movie_id=movie_id)
        )
        db.commit()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _presign(client, token: str, **body: object) -> httpx.Response:
    return client.post(PRESIGN_URL, json=body, headers=_auth(token))


def _presign_avatar(
    client, token: str, size: int, content_type: str = "image/png"
) -> httpx.Response:
    return _presign(
        client,
        token,
        purpose="avatar",
        filename="me.png",
        contentType=content_type,
        size=size,
    )


def _upload(
    client,
    presigned: dict,
    payload: bytes,
    *,
    content_type: str | None = None,
) -> httpx.Response:
    """One raw PUT to the signed url; no bearer token and no encryption session."""
    headers = dict(presigned["headers"])
    if content_type is not None:
        headers["Content-Type"] = content_type
    return client.put(presigned["uploadUrl"], content=payload, headers=headers)


def _complete(client, token: str, asset_id: int) -> httpx.Response:
    return client.post(COMPLETE_URL.format(asset_id=asset_id), headers=_auth(token))


def _asset(asset_id: int) -> MediaAsset | None:
    with SessionLocal() as db:
        return db.get(MediaAsset, asset_id)


def _all_assets() -> list[MediaAsset]:
    with SessionLocal() as db:
        return list(db.scalars(select(MediaAsset).order_by(MediaAsset.id)))


def _stored_files() -> set[Path]:
    """Every file under the upload directory, .part leftovers included."""
    root = Path(app.state.media_settings.upload_dir)
    return {path for path in root.rglob("*") if path.is_file()}


def _token_of(presigned: dict) -> str:
    """The `token` query value the presign response put into its upload url."""
    return presigned["uploadUrl"].split("token=", 1)[1]


def test_avatar_presign_upload_and_complete_round_trip(client) -> None:
    user_id, token = _create_user("+919000005001")

    presigned = _presign_avatar(client, token, len(AVATAR_PAYLOAD))

    assert presigned.status_code == 201
    body = presigned.json()
    assert set(body) == PRESIGN_KEYS
    assert body["method"] == "PUT"
    assert body["expiresIn"] == app.state.media_settings.presign_expires_seconds
    upload_url = body["uploadUrl"]
    assert upload_url.startswith(f"/api/media/{body['assetId']}/upload?token=")
    assert body["headers"]["Content-Type"] == "image/png"

    uploaded = _upload(client, body, AVATAR_PAYLOAD)

    assert uploaded.status_code == 204
    assert uploaded.content == b""

    completed = _complete(client, token, body["assetId"])

    assert completed.status_code == 200
    metadata = completed.json()
    assert set(metadata) == COMPLETE_KEYS
    assert metadata["assetId"] == body["assetId"]
    assert metadata["sha256"] == hashlib.sha256(AVATAR_PAYLOAD).hexdigest()
    assert metadata["size"] == len(AVATAR_PAYLOAD)
    assert metadata["contentType"] == "image/png"
    assert metadata["kind"] == MediaKind.AVATAR.value
    assert metadata["verified"] is True
    assert metadata["url"].startswith("/media/avatars/")

    served = client.get(metadata["url"])

    assert served.status_code == 200
    assert served.content == AVATAR_PAYLOAD

    asset = _asset(body["assetId"])
    assert asset is not None
    assert asset.kind == MediaKind.AVATAR.value
    assert asset.user_id == user_id
    assert asset.movie_id is None
    assert asset.review_id is None
    assert asset.is_current is True
    assert asset.verified is True
    assert asset.sha256 == metadata["sha256"]
    assert asset.size_bytes == len(AVATAR_PAYLOAD)


def test_review_photo_presign_links_the_review(client) -> None:
    owner_id, token = _create_user("+919000005002")
    _record_review(review_id=REVIEW_ID, user_id=owner_id, movie_id=MOVIE_ID)

    presigned = _presign(
        client,
        token,
        purpose="review_photo",
        ownerId=REVIEW_ID,
        filename="photo.png",
        contentType="image/png",
        size=len(REVIEW_PHOTO_PAYLOAD),
    )

    assert presigned.status_code == 201
    body = presigned.json()
    assert set(body) == PRESIGN_KEYS

    uploaded = _upload(client, body, REVIEW_PHOTO_PAYLOAD)
    assert uploaded.status_code == 204

    completed = _complete(client, token, body["assetId"])

    assert completed.status_code == 200
    metadata = completed.json()
    assert set(metadata) == COMPLETE_KEYS
    assert metadata["kind"] == MediaKind.REVIEW_PHOTO.value
    assert metadata["sha256"] == hashlib.sha256(REVIEW_PHOTO_PAYLOAD).hexdigest()
    assert metadata["size"] == len(REVIEW_PHOTO_PAYLOAD)
    assert metadata["url"].startswith("/media/review-photos/")

    asset = _asset(body["assetId"])
    assert asset is not None
    assert asset.kind == MediaKind.REVIEW_PHOTO.value
    assert asset.review_id == REVIEW_ID
    assert asset.movie_id == MOVIE_ID
    assert asset.user_id is None
    assert asset.is_current is True
    assert asset.verified is True


def test_review_photo_presign_for_another_users_review_is_403(client) -> None:
    author_id, _ = _create_user("+919000005003")
    _, stranger_token = _create_user("+919000005004")
    _record_review(review_id=REVIEW_ID, user_id=author_id, movie_id=MOVIE_ID)

    response = _presign(
        client,
        stranger_token,
        purpose="review_photo",
        ownerId=REVIEW_ID,
        filename="photo.png",
        contentType="image/png",
        size=len(REVIEW_PHOTO_PAYLOAD),
    )

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == REVIEW_OWNER_FORBIDDEN_DETAIL
    assert body["instance"] == PRESIGN_URL
    assert _all_assets() == []

    # A review this API never recorded has no author either, so it is refused too.
    unknown = _presign(
        client,
        stranger_token,
        purpose="review_photo",
        ownerId=REVIEW_ID + 1000,
        filename="photo.png",
        contentType="image/png",
        size=len(REVIEW_PHOTO_PAYLOAD),
    )

    assert unknown.status_code == 403
    assert unknown.json()["detail"] == REVIEW_OWNER_FORBIDDEN_DETAIL
    assert _all_assets() == []


def test_expired_and_tampered_upload_tokens_are_403(client) -> None:
    _, token = _create_user("+919000005005")
    presigned = _presign_avatar(client, token, len(AVATAR_PAYLOAD))
    assert presigned.status_code == 201
    body = presigned.json()
    asset_id = body["assetId"]
    secret = app.state.media_settings.presign_secret
    before = _stored_files()

    expired_token = sign_presign_token(
        asset_id=asset_id, expires_at=int(time.time()) - 1, secret=secret
    )
    expired = client.put(
        f"/api/media/{asset_id}/upload?token={expired_token}",
        content=AVATAR_PAYLOAD,
        headers={"Content-Type": "image/png"},
    )

    assert expired.status_code == 403
    expired_body = expired.json()
    assert set(expired_body) == PROBLEM_DETAIL_KEYS
    assert expired_body["title"] == "Forbidden"
    assert expired_body["status"] == 403
    assert expired_body["detail"] == PRESIGN_FORBIDDEN_DETAIL
    assert expired_body["instance"] == f"/api/media/{asset_id}/upload"

    expires_at, _, signature = _token_of(body).partition(".")
    flipped = "A" if signature[0] != "A" else "B"
    tampered_token = f"{expires_at}.{flipped}{signature[1:]}"
    tampered = client.put(
        f"/api/media/{asset_id}/upload?token={tampered_token}",
        content=AVATAR_PAYLOAD,
        headers={"Content-Type": "image/png"},
    )

    assert tampered.status_code == 403
    assert tampered.json()["detail"] == PRESIGN_FORBIDDEN_DETAIL

    # A refused upload stores nothing, so the asset still has no bytes to serve.
    assert _stored_files() == before
    asset = _asset(asset_id)
    assert asset is not None
    assert asset.sha256 == ""
    assert client.get(DOWNLOAD_URL.format(asset_id=asset_id)).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [AVATAR_PAYLOAD[:-1], AVATAR_PAYLOAD + b"extra-bytes"],
    ids=["fewer-bytes-than-declared", "more-bytes-than-declared"],
)
def test_a_size_mismatch_is_422_at_complete(client, payload: bytes) -> None:
    _, token = _create_user("+919000005006")
    presigned = _presign_avatar(client, token, len(AVATAR_PAYLOAD))
    assert presigned.status_code == 201
    body = presigned.json()

    uploaded = _upload(client, body, payload)
    assert uploaded.status_code == 204

    completed = _complete(client, token, body["assetId"])

    assert completed.status_code == 422
    problem = completed.json()
    assert set(problem) == PROBLEM_DETAIL_KEYS
    assert problem["type"] == "about:blank"
    assert problem["status"] == 422
    assert problem["detail"] == SIZE_MISMATCH_DETAIL
    assert problem["instance"] == COMPLETE_URL.format(asset_id=body["assetId"])

    asset = _asset(body["assetId"])
    assert asset is not None
    assert asset.verified is False
    assert asset.is_current is False


def test_an_upload_with_the_wrong_content_type_is_422(client) -> None:
    _, token = _create_user("+919000005007")
    presigned = _presign_avatar(client, token, len(AVATAR_PAYLOAD))
    assert presigned.status_code == 201
    body = presigned.json()
    before = _stored_files()

    uploaded = _upload(client, body, AVATAR_PAYLOAD, content_type="image/jpeg")

    assert uploaded.status_code == 422
    problem = uploaded.json()
    assert set(problem) == PROBLEM_DETAIL_KEYS
    assert problem["status"] == 422
    assert problem["detail"] == TYPE_MISMATCH_DETAIL
    assert problem["instance"] == f"/api/media/{body['assetId']}/upload"
    assert _stored_files() == before

    # Nothing was stored, so completing it cannot succeed either.
    completed = _complete(client, token, body["assetId"])

    assert completed.status_code == 422
    assert completed.json()["detail"] == INCOMPLETE_ASSET_DETAIL


def test_completing_before_uploading_is_422(client) -> None:
    _, token = _create_user("+919000005008")
    presigned = _presign_avatar(client, token, len(AVATAR_PAYLOAD))
    assert presigned.status_code == 201
    asset_id = presigned.json()["assetId"]

    completed = _complete(client, token, asset_id)

    assert completed.status_code == 422
    body = completed.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["status"] == 422
    assert body["detail"] == INCOMPLETE_ASSET_DETAIL

    served = client.get(DOWNLOAD_URL.format(asset_id=asset_id))

    assert served.status_code == 404
    assert served.json()["detail"] == INCOMPLETE_ASSET_DETAIL


def test_completing_an_unknown_asset_is_404(client) -> None:
    _, token = _create_user("+919000005009")

    completed = _complete(client, token, UNKNOWN_ASSET_ID)

    assert completed.status_code == 404
    body = completed.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == f"Media asset not found: {UNKNOWN_ASSET_ID}"
    assert body["instance"] == COMPLETE_URL.format(asset_id=UNKNOWN_ASSET_ID)


def test_presign_enforces_the_kind_limits(client) -> None:
    _, token = _create_user("+919000005010")
    limit = app.state.media_settings.max_image_bytes
    before = _stored_files()

    too_large = _presign_avatar(client, token, limit + 1)
    unsupported = _presign_avatar(client, token, len(AVATAR_PAYLOAD), "image/gif")
    no_owner = _presign(
        client,
        token,
        purpose="review_photo",
        filename="photo.png",
        contentType="image/png",
        size=len(REVIEW_PHOTO_PAYLOAD),
    )

    for response in (too_large, unsupported, no_owner):
        assert response.status_code == 422
        assert set(response.json()) == PROBLEM_DETAIL_KEYS
        assert response.json()["instance"] == PRESIGN_URL

    assert unsupported.json()["detail"] == TYPE_MISMATCH_DETAIL
    assert _all_assets() == []
    assert _stored_files() == before


def test_a_second_avatar_demotes_the_first(client) -> None:
    user_id, token = _create_user("+919000005011")

    first = _presign_avatar(client, token, len(AVATAR_PAYLOAD))
    assert first.status_code == 201
    first_body = first.json()
    assert _upload(client, first_body, AVATAR_PAYLOAD).status_code == 204
    assert _complete(client, token, first_body["assetId"]).status_code == 200

    second = _presign_avatar(client, token, len(SECOND_AVATAR_PAYLOAD))
    assert second.status_code == 201
    second_body = second.json()
    assert _upload(client, second_body, SECOND_AVATAR_PAYLOAD).status_code == 204
    completed = _complete(client, token, second_body["assetId"])

    assert completed.status_code == 200
    assert completed.json()["sha256"] == hashlib.sha256(
        SECOND_AVATAR_PAYLOAD
    ).hexdigest()

    previous = _asset(first_body["assetId"])
    current = _asset(second_body["assetId"])

    assert previous is not None
    assert current is not None
    assert previous.is_current is False
    assert current.is_current is True
    # The superseded avatar keeps its row and its bytes, exactly as a poster does.
    assert previous.kind == MediaKind.AVATAR.value
    assert current.kind == MediaKind.AVATAR.value
    assert previous.user_id == user_id
    assert current.user_id == user_id
    assert client.get(previous.url).content == AVATAR_PAYLOAD
    assert client.get(current.url).content == SECOND_AVATAR_PAYLOAD
