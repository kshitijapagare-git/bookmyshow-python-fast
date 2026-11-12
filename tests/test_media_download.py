"""Downloading a stored asset's bytes and reading its metadata by asset id.

Every upload still goes through the internal route, so the bytes a download returns can
be compared with what was actually posted, and the MEDIA_UPLOAD_DIR that tests/conftest.py
points at a fresh temp directory lets a refusal delete the file behind a row or tamper
with its `storage_key` before the request. Downloads are public: no token is sent.
"""

import hashlib
from pathlib import Path

import httpx
import pytest

from app.db import SessionLocal
from app.main import app
from app.models import MediaAsset
from app.security import MEDIA_SERVICE_TOKEN

UPLOAD_URL = "/api/movies/{movie_id}/media"
DOWNLOAD_URL = "/api/media/{asset_id}"
METADATA_URL = "/api/media/{asset_id}/metadata"
TOKEN_HEADER = "X-Internal-Token"
ASSET_KEYS = {"assetId", "kind", "url", "size", "contentType", "sha256"}
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
MOVIE_ID = 1
UNKNOWN_ASSET_ID = 999999
POSTER_PAYLOAD = b"\x89PNG\r\n\x1a\nposter-bytes"
SECOND_POSTER_PAYLOAD = b"\x89PNG\r\n\x1a\nsecond-poster-bytes"
BACKDROP_PAYLOAD = b"\xff\xd8\xff\xe0backdrop-bytes"
TRAILER_PAYLOAD = b"\x00\x00\x00\x18ftypmp42trailer-bytes"
ESCAPING_STORAGE_KEY = "../../etc/passwd"


def _upload(
    client,
    movie_id: int,
    kind: str,
    payload: bytes,
    content_type: str,
    token: str | None = MEDIA_SERVICE_TOKEN,
) -> httpx.Response:
    """One multipart upload; the token header is left off entirely when it is None."""
    headers = {} if token is None else {TOKEN_HEADER: token}
    return client.post(
        UPLOAD_URL.format(movie_id=movie_id),
        files={"file": ("asset", payload, content_type)},
        data={"kind": kind},
        headers=headers,
    )


def _download_url(asset_id: int) -> str:
    return DOWNLOAD_URL.format(asset_id=asset_id)


def _metadata_url(asset_id: int) -> str:
    return METADATA_URL.format(asset_id=asset_id)


def _asset_row(asset_id: int) -> MediaAsset:
    """The stored row, read through its own session so a test can unlink or tamper with it."""
    with SessionLocal() as db:
        row = db.get(MediaAsset, asset_id)
        assert row is not None
        return row


def _remove_stored_file(asset_id: int) -> None:
    """Delete the bytes behind a row, leaving the row itself in place."""
    row = _asset_row(asset_id)
    (Path(app.state.media_settings.upload_dir) / row.storage_key).unlink()


def _set_storage_key(asset_id: int, storage_key: str) -> None:
    """Point a row's `storage_key` somewhere it was never uploaded to."""
    with SessionLocal() as db:
        row = db.get(MediaAsset, asset_id)
        assert row is not None
        row.storage_key = storage_key
        db.commit()


def _assert_asset_is_gone(client, asset_id: int) -> None:
    """Both GET routes answer the same 404 problem detail for this asset id."""
    for url in (_download_url(asset_id), _metadata_url(asset_id)):
        response = client.get(url)

        assert response.status_code == 404, url
        body = response.json()
        assert set(body) == PROBLEM_DETAIL_KEYS
        assert body["type"] == "about:blank"
        assert body["title"] == "Not Found"
        assert body["status"] == 404
        assert body["detail"] == f"Media asset not found: {asset_id}"
        assert body["instance"] == url


@pytest.mark.parametrize(
    ("kind", "content_type", "payload"),
    [
        ("POSTER", "image/png", POSTER_PAYLOAD),
        ("BACKDROP", "image/jpeg", BACKDROP_PAYLOAD),
        ("TRAILER", "video/mp4", TRAILER_PAYLOAD),
    ],
)
def test_download_returns_the_uploaded_bytes_and_headers(
    client, kind: str, content_type: str, payload: bytes
) -> None:
    uploaded = _upload(client, MOVIE_ID, kind, payload, content_type)
    assert uploaded.status_code == 201
    asset_id = uploaded.json()["assetId"]

    response = client.get(_download_url(asset_id))

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == content_type
    assert response.headers["content-length"] == str(len(payload))
    disposition = response.headers.get("content-disposition", "")
    assert disposition.startswith("inline")


def test_a_superseded_asset_is_still_downloadable(client) -> None:
    first = _upload(client, MOVIE_ID, "POSTER", POSTER_PAYLOAD, "image/png")
    second = _upload(client, MOVIE_ID, "POSTER", SECOND_POSTER_PAYLOAD, "image/png")
    assert first.status_code == 201
    assert second.status_code == 201
    first_id = first.json()["assetId"]
    second_id = second.json()["assetId"]

    rows = [_asset_row(first_id), _asset_row(second_id)]
    assert [row.is_current for row in rows] == [False, True]

    first_download = client.get(_download_url(first_id))
    second_download = client.get(_download_url(second_id))

    assert first_download.status_code == 200
    assert first_download.content == POSTER_PAYLOAD
    assert second_download.status_code == 200
    assert second_download.content == SECOND_POSTER_PAYLOAD


def test_metadata_returns_the_asset_fields_without_the_storage_key(client) -> None:
    payload = BACKDROP_PAYLOAD
    uploaded = _upload(client, MOVIE_ID, "BACKDROP", payload, "image/jpeg")
    assert uploaded.status_code == 201
    asset = uploaded.json()

    response = client.get(_metadata_url(asset["assetId"]))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == ASSET_KEYS
    assert body == {
        "assetId": asset["assetId"],
        "kind": "BACKDROP",
        "url": asset["url"],
        "size": len(payload),
        "contentType": "image/jpeg",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }

    # Neither the raw bytes nor the storage key the row keeps may reach the client.
    # The mandated `url` is built from this key, so the key legitimately appears inside
    # that one field; nothing else in the body may carry it.
    stored_key = _asset_row(asset["assetId"]).storage_key
    assert stored_key
    assert stored_key not in response.text.replace(asset["url"], "")
    assert "storageKey" not in body
    assert "storage_key" not in body


def test_an_unknown_or_deleted_asset_is_404(client) -> None:
    _assert_asset_is_gone(client, UNKNOWN_ASSET_ID)

    # A row whose bytes were removed from storage reads exactly like an unknown id.
    unlinked = _upload(client, MOVIE_ID, "POSTER", POSTER_PAYLOAD, "image/png")
    assert unlinked.status_code == 201
    unlinked_id = unlinked.json()["assetId"]
    _remove_stored_file(unlinked_id)
    _assert_asset_is_gone(client, unlinked_id)

    # A tampered storage_key that escapes the upload directory is never served either.
    tampered = _upload(client, MOVIE_ID, "TRAILER", TRAILER_PAYLOAD, "video/mp4")
    assert tampered.status_code == 201
    tampered_id = tampered.json()["assetId"]
    _set_storage_key(tampered_id, ESCAPING_STORAGE_KEY)
    _assert_asset_is_gone(client, tampered_id)
