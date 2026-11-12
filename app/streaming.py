"""HTTP byte-range semantics for streaming a stored object to a client.

The trailer route serves a film's current `TRAILER` file, and a browser seeks by
asking for a `Range: bytes=start-end` slice of it. Those semantics live here rather
than in the router so the edge cases -- a suffix range, an open-ended range, an end
past the object, or anything the RFC does not allow -- are testable without going
through `TestClient`.

`parse_range_header` follows RFC 9110: it answers `None` when there is no Range
header at all (the whole body), a `ByteRange` with an inclusive end for exactly one
satisfiable `bytes` range, and raises `UnsatisfiableRangeError` for everything else.
A malformed or multi-part header and a syntactically valid but unsatisfiable one are
the same 416 outcome, so they share one exception type rather than two.

`iter_file_range` is the only place file bytes are read: one `open`, one `seek`, and
reads bounded by `STREAM_CHUNK_SIZE`, so a long video is never loaded whole into
memory and the client can start playing after the first chunk.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

STREAM_CHUNK_SIZE = 64 * 1024
RANGE_UNIT = "bytes"
SUFFIX_PREFIX = "-"


class UnsatisfiableRangeError(Exception):
    """The Range header names no slice of this object; answered as a 416."""

    def __init__(self, size: int) -> None:
        super().__init__(f"range not satisfiable for an object of {size} bytes")
        self.size = size

    @property
    def content_range(self) -> str:
        """The `Content-Range` a 416 carries: `bytes */{size}`."""
        return f"{RANGE_UNIT} */{self.size}"


@dataclass(frozen=True)
class ByteRange:
    """One inclusive byte range of an object: `start` through `end`."""

    start: int
    end: int

    @property
    def length(self) -> int:
        """How many bytes the range spans, inclusive of both ends."""
        return self.end - self.start + 1


def _offset(raw: str, size: int) -> int:
    """A non-negative decimal offset, or UnsatisfiableRangeError for anything else."""
    if not raw.isdigit():
        raise UnsatisfiableRangeError(size)
    return int(raw)


def parse_range_header(header: str | None, size: int) -> ByteRange | None:
    """The slice a `Range` header asks for; None means send the whole object.

    Raises `UnsatisfiableRangeError` for a missing or wrong unit, a multi-part range,
    a malformed specifier, a start past its end, a start at or past the object and a
    zero-length suffix. An end past the object is clamped, and a suffix longer than
    the object is the whole object, both per RFC 9110.
    """
    if header is None:
        return None
    spec = header.strip()
    if not spec:
        return None

    if size <= 0 or "," in spec:
        raise UnsatisfiableRangeError(size)

    unit, separator, specifier = spec.partition("=")
    if not separator or unit.strip().lower() != RANGE_UNIT:
        raise UnsatisfiableRangeError(size)

    start_text, dash, end_text = specifier.strip().partition(SUFFIX_PREFIX)
    if not dash:
        raise UnsatisfiableRangeError(size)

    if start_text == "":
        # A suffix range: the last N bytes, or the whole object when N exceeds it.
        suffix = _offset(end_text, size)
        if suffix == 0:
            raise UnsatisfiableRangeError(size)
        if suffix >= size:
            return ByteRange(start=0, end=size - 1)
        return ByteRange(start=size - suffix, end=size - 1)

    start = _offset(start_text, size)
    if start >= size:
        raise UnsatisfiableRangeError(size)
    if end_text == "":
        return ByteRange(start=start, end=size - 1)

    end = _offset(end_text, size)
    if end < start:
        raise UnsatisfiableRangeError(size)
    return ByteRange(start=start, end=min(end, size - 1))


def iter_file_range(
    path: Path,
    byte_range: ByteRange | None,
    *,
    chunk_size: int = STREAM_CHUNK_SIZE,
) -> Iterator[bytes]:
    """Yield the requested slice of `path`, at most `chunk_size` bytes per read.

    The file is opened once and seeked to the range's start, so only the requested
    bytes are ever read and the object is never loaded whole into memory. With no
    range the whole file is streamed to its end.
    """
    if byte_range is None:
        with path.open("rb") as whole:
            while True:
                chunk = whole.read(chunk_size)
                if not chunk:
                    return
                yield chunk

    remaining = byte_range.length
    if remaining <= 0:
        return

    with path.open("rb") as source:
        source.seek(byte_range.start)
        while remaining > 0:
            chunk = source.read(min(chunk_size, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk
