"""Byte-range parsing and bounded file reading: the semantics behind the trailer route.

`parse_range_header` is pinned over every valid form and every unusable one, and
`iter_file_range` is exercised with a payload several times `STREAM_CHUNK_SIZE` so a
whole-object read would show up as one oversized chunk -- or as a `Path.read_bytes`
call the test monkeypatches to fail.
"""

from pathlib import Path

import pytest

from app.streaming import (
    STREAM_CHUNK_SIZE,
    ByteRange,
    UnsatisfiableRangeError,
    iter_file_range,
    parse_range_header,
)

SIZE = 1000
LARGE_PAYLOAD = bytes(range(256)) * 800


def _refuse_whole_read(*args: object, **kwargs: object) -> bytes:
    """A stand-in for Path.read_bytes: streaming must never load the object whole."""
    raise AssertionError("the whole object must never be read into memory")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=0-3", ByteRange(start=0, end=3)),
        ("bytes=0-999", ByteRange(start=0, end=999)),
        ("bytes=100-", ByteRange(start=100, end=999)),
        ("bytes=-100", ByteRange(start=900, end=999)),
        ("bytes=-1000", ByteRange(start=0, end=999)),
        ("bytes=-5000", ByteRange(start=0, end=999)),
        ("bytes=0-5000", ByteRange(start=0, end=999)),
        ("  bytes=10-19  ", ByteRange(start=10, end=19)),
        ("BYTES=10-19", ByteRange(start=10, end=19)),
    ],
)
def test_parse_range_header_covers_the_valid_forms(
    header: str, expected: ByteRange
) -> None:
    parsed = parse_range_header(header, SIZE)

    assert parsed == expected
    assert parsed is not None
    assert parsed.length == expected.end - expected.start + 1


def test_parse_range_header_returns_none_without_a_header() -> None:
    assert parse_range_header(None, SIZE) is None
    assert parse_range_header("", SIZE) is None
    assert parse_range_header("   ", SIZE) is None


@pytest.mark.parametrize(
    "header",
    [
        "bytes=1000-",
        "bytes=5-2",
        "bytes=-0",
        "bytes=abc",
        "items=0-3",
        "bytes=0-1,3-4",
        "0-3",
        "bytes=",
        "bytes=-",
    ],
)
def test_parse_range_header_rejects_every_invalid_form(header: str) -> None:
    with pytest.raises(UnsatisfiableRangeError) as raised:
        parse_range_header(header, SIZE)

    assert raised.value.size == SIZE
    assert raised.value.content_range == f"bytes */{SIZE}"


def test_unsatisfiable_range_error_reports_the_object_size() -> None:
    error = UnsatisfiableRangeError(1000)

    assert error.size == 1000
    assert error.content_range == "bytes */1000"


def test_iter_file_range_never_reads_the_whole_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "trailer.mp4"
    path.write_bytes(LARGE_PAYLOAD)
    assert len(LARGE_PAYLOAD) > 2 * STREAM_CHUNK_SIZE
    monkeypatch.setattr(Path, "read_bytes", _refuse_whole_read)

    chunks = list(iter_file_range(path, None))

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= STREAM_CHUNK_SIZE for chunk in chunks)
    assert b"".join(chunks) == LARGE_PAYLOAD


def test_iter_file_range_yields_only_the_requested_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "trailer.mp4"
    path.write_bytes(LARGE_PAYLOAD)
    monkeypatch.setattr(Path, "read_bytes", _refuse_whole_read)

    chunks = list(
        iter_file_range(path, ByteRange(start=100, end=199), chunk_size=16)
    )

    assert all(0 < len(chunk) <= 16 for chunk in chunks)
    assert len(chunks) == 7
    assert b"".join(chunks) == LARGE_PAYLOAD[100:200]


def test_iter_file_range_yields_nothing_for_a_zero_length_range(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trailer.mp4"
    path.write_bytes(b"0123456789")

    assert list(iter_file_range(path, ByteRange(start=5, end=4))) == []
