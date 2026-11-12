"""Downloading a stored asset's bytes and metadata through the public media routes.

The upload route is the only way bytes reach storage, so every test uploads through
`POST /api/movies/{movieId}/media` with the internal token and reads the asset back by
the `assetId` that upload answered. Every upload lands in the MEDIA_UPLOAD_DIR
tests/conftest.py points at a fresh temp directory, so the bytes fetched here are the
ones the upload just wrote.
"""

import hashlib

import pytest

from app.security import MEDIA_SERVICE_TOKEN

UPLOAD_URL = "/api/movies/{movie_id}/media"
DOWNLOAD_URL = "/api/media/{asset_id}"
METADATA_URL = "/api/media/{asset_id}/metadata"
TOKEN_HEADER = "X-Internal-Token"
ASSET_KEYS = {"assetId", "kind", "url", "size", "contentType", "sha256"}
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
MOVIE_ID = 1
POSTER_PAYLOAD = b"\x89PNG\r\n\x1a\nposter-bytes"
SECOND_POSTER_PAYLOAD = b"\x89PNG\r\n\x1a\nsecond-poster-bytes"
BACKDROP_PAYLOAD = b"\xff\xd8\xff\xe0backdrop-bytes"
TRAILER_PAYLOAD = b"\x00\x00\x00\x18ftypmp42trailer-bytes"


def _upload(
    client,
    kind: str,
    payload: bytes,
    content_type: str,
    movie_id: int = MOVIE_ID,
) -> dict:
    """One authenticated upload; answers the asset metadata the upload route returned."""
    response = client.post(
        UPLOAD_URL.format(movie_id=movie_id),
        files={"file": ("asset", payload, content_type)},
        data={"kind": kind},
        headers={TOKEN_HEADER: MEDIA_SERVICE_TOKEN},
    )
    assert response.status_code == 201
    return response.json()


def _download(client, asset_id: int):
    return client.get(DOWNLOAD_URL.format(asset_id=asset_id))


def _metadata(client, asset_id: int):
    return client.get(METADATA_URL.format(asset_id=asset_id))


@pytest.mark.parametrize(
    ("kind", "content_type", "payload"),
    [
        ("POSTER", "image/png", POSTER_PAYLOAD),
        ("BACKDROP", "image/jpeg", BACKDROP_PAYLOAD),
        ("TRAILER", "video/mp4", TRAILER_PAYLOAD),
    ],
)
def test_download_returns_the_stored_bytes_and_headers(
    client, kind: str, content_type: str, payload: bytes
) -> None:
    asset = _upload(client, kind, payload, content_type)

    response = _download(client, asset["assetId"])

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == content_type
    assert response.headers["content-length"] == str(len(payload))
    assert response.headers["content-disposition"].startswith("inline")


def test_download_needs_no_token(client) -> None:
    asset = _upload(client, "POSTER", POSTER_PAYLOAD, "image/png")

    response = client.get(
        DOWNLOAD_URL.format(asset_id=asset["assetId"]),
        headers={},
    )

    assert response.status_code == 200
    assert response.content == POSTER_PAYLOAD


def test_a_superseded_asset_is_still_downloadable(client) -> None:
    first = _upload(client, "POSTER", POSTER_PAYLOAD, "image/png")
    second = _upload(client, "POSTER", SECOND_POSTER_PAYLOAD, "image/png")
    assert first["assetId"] != second["assetId"]

    superseded = _download(client, first["assetId"])
    current = _download(client, second["assetId"])

    assert superseded.status_code == 200
    assert superseded.content == POSTER_PAYLOAD
    assert current.status_code == 200
    assert current.content == SECOND_POSTER_PAYLOAD


def test_download_unknown_asset_is_404(client) -> None:
    response = _download(client, 999999)

    assert response.status_code == 404
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == "Media asset not found: 999999"
    assert body["instance"] == "/api/media/999999"


@pytest.mark.parametrize(
    ("kind", "content_type", "payload"),
    [
        ("POSTER", "image/png", POSTER_PAYLOAD),
        ("BACKDROP", "image/jpeg", BACKDROP_PAYLOAD),
        ("TRAILER", "video/mp4", TRAILER_PAYLOAD),
    ],
)
def test_metadata_returns_the_asset_shape(
    client, kind: str, content_type: str, payload: bytes
) -> None:
    asset = _upload(client, kind, payload, content_type)

    response = _metadata(client, asset["assetId"])

    assert response.status_code == 200
    body = response.json()
    assert set(body) == ASSET_KEYS
    assert body["assetId"] == asset["assetId"]
    assert body["kind"] == kind
    assert body["url"] == asset["url"]
    assert body["size"] == len(payload)
    assert body["contentType"] == content_type
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()
    # Neither the storage key nor the bytes themselves belong in the metadata.
    assert "storageKey" not in body
    assert "storage_key" not in body
    assert "content" not in body


def test_metadata_needs_no_token(client) -> None:
    asset = _upload(client, "POSTER", POSTER_PAYLOAD, "image/png")

    response = client.get(
        METADATA_URL.format(asset_id=asset["assetId"]),
        headers={},
    )

    assert response.status_code == 200
    assert response.json()["assetId"] == asset["assetId"]


def test_metadata_for_a_superseded_asset_still_resolves(client) -> None:
    first = _upload(client, "POSTER", POSTER_PAYLOAD, "image/png")
    second = _upload(client, "POSTER", SECOND_POSTER_PAYLOAD, "image/png")
    assert first["assetId"] != second["assetId"]

    superseded = _metadata(client, first["assetId"])
    current = _metadata(client, second["assetId"])

    assert superseded.status_code == 200
    assert superseded.json()["assetId"] == first["assetId"]
    assert superseded.json()["url"] == first["url"]
    assert superseded.json()["sha256"] == hashlib.sha256(POSTER_PAYLOAD).hexdigest()

    assert current.status_code == 200
    assert current.json()["assetId"] == second["assetId"]
    assert current.json()["url"] == second["url"]
    assert current.json()["sha256"] == hashlib.sha256(
        SECOND_POSTER_PAYLOAD
    ).hexdigest()


def test_metadata_unknown_asset_is_404(client) -> None:
    response = _metadata(client, 999999)

    assert response.status_code == 404
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == "Media asset not found: 999999"
    assert body["instance"] == "/api/media/999999/metadata"
