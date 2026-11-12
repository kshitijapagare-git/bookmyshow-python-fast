"""Streaming a movie's current trailer, including every HTTP byte range it answers.

Every upload goes through the internal media route into the fresh MEDIA_UPLOAD_DIR
tests/conftest.py points at, exactly as tests/test_movie_media.py does it, so the bytes
the streaming route serves are the ones a trailer upload really wrote -- and a range
request can be checked against the same payload slice by slice.
"""

from pathlib import Path

import httpx
import pytest

from app.security import MEDIA_SERVICE_TOKEN

MEDIA_URL = "/api/movies/{movie_id}/media"
TRAILER_URL = "/api/movies/{movie_id}/trailer"
TOKEN_HEADER = "X-Internal-Token"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
MOVIE_ID = 1
UNKNOWN_MOVIE_ID = 999999
MP4_PREFIX = b"\x00\x00\x00\x18ftypmp42"
TRAILER_PAYLOAD = MP4_PREFIX + bytes(range(256)) * 8
SECOND_TRAILER_PAYLOAD = MP4_PREFIX + bytes(range(255, -1, -1)) * 8
# Several times STREAM_CHUNK_SIZE, so a whole-object read would be a single big chunk.
LARGE_PAYLOAD = MP4_PREFIX + bytes(range(256)) * 1200


def _upload_trailer(client, movie_id: int, payload: bytes) -> httpx.Response:
    """One authenticated multipart trailer upload, mirroring tests/test_movie_media.py."""
    return client.post(
        MEDIA_URL.format(movie_id=movie_id),
        files={"file": ("trailer", payload, "video/mp4")},
        data={"kind": "TRAILER"},
        headers={TOKEN_HEADER: MEDIA_SERVICE_TOKEN},
    )


def _stream(
    client, movie_id: int = MOVIE_ID, range_header: str | None = None
) -> httpx.Response:
    """Fetch the trailer route; the Range header is left off entirely when it is None."""
    headers = {} if range_header is None else {"Range": range_header}
    return client.get(TRAILER_URL.format(movie_id=movie_id), headers=headers)


def _url(movie_id: int = MOVIE_ID) -> str:
    return TRAILER_URL.format(movie_id=movie_id)


def _refuse_whole_read(*args: object, **kwargs: object) -> bytes:
    """A stand-in for Path.read_bytes: the trailer must never be loaded whole."""
    raise AssertionError("the trailer must never be read whole into memory")


def _refuse_whole_read_text(*args: object, **kwargs: object) -> str:
    """A stand-in for Path.read_text, for the same reason."""
    raise AssertionError("the trailer must never be read whole into memory")


def test_full_trailer_stream_serves_the_whole_video(client) -> None:
    uploaded = _upload_trailer(client, MOVIE_ID, TRAILER_PAYLOAD)

    assert uploaded.status_code == 201
    assert uploaded.json()["kind"] == "TRAILER"

    response = _stream(client)

    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-length"] == str(len(TRAILER_PAYLOAD))
    assert response.content == TRAILER_PAYLOAD


def test_byte_range_suffix_and_open_ended_requests(client) -> None:
    uploaded = _upload_trailer(client, MOVIE_ID, TRAILER_PAYLOAD)

    assert uploaded.status_code == 201
    size = len(TRAILER_PAYLOAD)

    first_four = _stream(client, range_header="bytes=0-3")

    assert first_four.status_code == 206
    assert first_four.headers["accept-ranges"] == "bytes"
    assert first_four.headers["content-range"] == f"bytes 0-3/{size}"
    assert first_four.headers["content-length"] == "4"
    assert first_four.content == TRAILER_PAYLOAD[:4]

    open_ended = _stream(client, range_header="bytes=100-")

    assert open_ended.status_code == 206
    assert open_ended.headers["content-range"] == f"bytes 100-{size - 1}/{size}"
    assert open_ended.headers["content-length"] == str(size - 100)
    assert open_ended.content == TRAILER_PAYLOAD[100:]

    suffix = _stream(client, range_header="bytes=-100")

    assert suffix.status_code == 206
    assert suffix.headers["content-range"] == f"bytes {size - 100}-{size - 1}/{size}"
    assert suffix.headers["content-length"] == "100"
    assert suffix.content == TRAILER_PAYLOAD[-100:]

    # A suffix longer than the object is the whole object, end clamped like RFC 9110.
    oversized_suffix = _stream(client, range_header=f"bytes=-{size + 50}")

    assert oversized_suffix.status_code == 206
    assert oversized_suffix.headers["content-range"] == f"bytes 0-{size - 1}/{size}"
    assert oversized_suffix.headers["content-length"] == str(size)
    assert oversized_suffix.content == TRAILER_PAYLOAD

    # An end past the object is clamped to the last byte rather than refused.
    clamped = _stream(client, range_header=f"bytes=0-{size + 50}")

    assert clamped.status_code == 206
    assert clamped.headers["content-range"] == f"bytes 0-{size - 1}/{size}"
    assert clamped.content == TRAILER_PAYLOAD


@pytest.mark.parametrize(
    "template",
    [
        "bytes={size}-",
        "bytes=5-2",
        "bytes=-0",
        "bytes=abc",
        "items=0-3",
        "bytes=0-1,3-4",
    ],
)
def test_unusable_ranges_are_416_with_the_problem_detail_body(
    client, template: str
) -> None:
    uploaded = _upload_trailer(client, MOVIE_ID, TRAILER_PAYLOAD)
    assert uploaded.status_code == 201
    size = len(TRAILER_PAYLOAD)

    response = _stream(client, range_header=template.format(size=size))

    assert response.status_code == 416
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"]
    assert body["status"] == 416
    assert body["detail"]
    assert body["instance"] == _url()


def test_missing_or_absent_trailers_are_the_two_distinct_404s(client) -> None:
    # A seeded movie has a stored trailer_url, but no MediaAsset row, so nothing streams.
    absent = _stream(client)

    assert absent.status_code == 404
    body = absent.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == f"Trailer not found: {MOVIE_ID}"
    assert body["instance"] == _url()

    unknown = _stream(client, movie_id=UNKNOWN_MOVIE_ID)

    assert unknown.status_code == 404
    unknown_body = unknown.json()
    assert set(unknown_body) == PROBLEM_DETAIL_KEYS
    assert unknown_body["status"] == 404
    assert unknown_body["detail"] == f"Movie not found: {UNKNOWN_MOVIE_ID}"
    assert unknown_body["instance"] == _url(UNKNOWN_MOVIE_ID)

    uploaded = _upload_trailer(client, MOVIE_ID, TRAILER_PAYLOAD)

    assert uploaded.status_code == 201
    served = _stream(client)
    assert served.status_code == 200
    assert served.content == TRAILER_PAYLOAD


def test_a_second_trailer_replaces_the_streamed_bytes(client) -> None:
    first = _upload_trailer(client, MOVIE_ID, TRAILER_PAYLOAD)
    second = _upload_trailer(client, MOVIE_ID, SECOND_TRAILER_PAYLOAD)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["url"] != second.json()["url"]

    response = _stream(client)

    assert response.status_code == 200
    assert response.content == SECOND_TRAILER_PAYLOAD
    assert response.headers["content-length"] == str(len(SECOND_TRAILER_PAYLOAD))


def test_streaming_does_not_read_the_whole_object(client, monkeypatch) -> None:
    monkeypatch.setattr(Path, "read_bytes", _refuse_whole_read)
    monkeypatch.setattr(Path, "read_text", _refuse_whole_read_text)

    uploaded = _upload_trailer(client, MOVIE_ID, LARGE_PAYLOAD)
    assert uploaded.status_code == 201

    response = _stream(client)

    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(LARGE_PAYLOAD))
    assert response.content == LARGE_PAYLOAD

    ranged = _stream(client, range_header="bytes=0-99")

    assert ranged.status_code == 206
    assert ranged.content == LARGE_PAYLOAD[:100]
