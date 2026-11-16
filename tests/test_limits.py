"""argus_cortex.limits: caps hold whatever the client declares or sends."""

from __future__ import annotations

import asyncio
import base64
import io

import pytest
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from argus_cortex.limits import BadInput, TooLarge, decode_base64, open_image, read_body, read_upload


class FakeRequest:
    def __init__(self, chunks: list[bytes], content_length: str | None = None):
        self.headers = {} if content_length is None else {"content-length": content_length}
        self.chunks, self.pulled = chunks, 0

    async def stream(self):
        for c in self.chunks:
            self.pulled += len(c)
            yield c


class FakeUpload:
    def __init__(self, data: bytes, size: int | None = None):
        self.buf, self.size, self.reads = io.BytesIO(data), size, []

    async def read(self, n: int = -1) -> bytes:
        self.reads.append(n)
        return self.buf.read(n)


def run(coro):
    return asyncio.run(coro)


# ── read_body ────────────────────────────────────────────────────────────────


def test_declared_length_over_the_cap_is_refused_before_reading() -> None:
    req = FakeRequest([b"x" * 10], content_length="1000000")
    with pytest.raises(TooLarge, match="larger than 100 bytes"):
        run(read_body(req, 100))
    assert req.pulled == 0


@pytest.mark.parametrize("bad", ["-1", "1e3", "ten", "", "0x10", "1, 2"])
def test_malformed_length_is_refused(bad: str) -> None:
    with pytest.raises(BadInput):
        run(read_body(FakeRequest([b"x"], content_length=bad), 100))


def test_a_lying_length_is_caught_while_streaming() -> None:
    req = FakeRequest([b"x" * 60, b"x" * 60, b"x" * 60], content_length="10")
    with pytest.raises(TooLarge):
        run(read_body(req, 100))
    assert req.pulled <= 160  # stopped at the first chunk past the cap


@given(st.lists(st.binary(max_size=50), max_size=12), st.integers(0, 300), st.booleans())
def test_body_returned_iff_within_the_cap(chunks: list[bytes], cap: int, declare: bool) -> None:
    total = b"".join(chunks)
    req = FakeRequest(chunks, content_length=str(len(total)) if declare else None)  # chunked when not declared
    try:
        got = run(read_body(req, cap))
    except TooLarge:
        assert len(total) > cap
        return
    assert got == total and len(got) <= cap


# ── read_upload ──────────────────────────────────────────────────────────────


def test_upload_reads_stop_at_the_cap() -> None:
    up = FakeUpload(b"y" * 10_000)
    with pytest.raises(TooLarge):
        run(read_upload(up, 1000, chunk=256))
    assert sum(n for n in up.reads) <= 1001 + 256


def test_upload_size_known_up_front() -> None:
    up = FakeUpload(b"", size=5000)
    with pytest.raises(TooLarge):
        run(read_upload(up, 1000))
    assert up.reads == []


@given(st.binary(max_size=3000), st.integers(0, 3000), st.integers(1, 700))
def test_upload_returned_iff_within_the_cap(data: bytes, cap: int, chunk: int) -> None:
    try:
        got = run(read_upload(FakeUpload(data), cap, chunk=chunk))
    except TooLarge:
        assert len(data) > cap
        return
    assert got == data


# ── decode_base64 ────────────────────────────────────────────────────────────


@given(st.binary(max_size=400), st.integers(0, 400), st.booleans())
def test_base64_round_trip_iff_within_the_cap(data: bytes, cap: int, as_data_uri: bool) -> None:
    enc = base64.b64encode(data).decode()
    if as_data_uri:
        enc = "data:image/png;base64," + enc
    try:
        got = decode_base64(enc, cap)
    except TooLarge:
        assert len(data) > cap
        return
    assert got == data


def test_oversized_base64_is_refused_without_decoding(monkeypatch) -> None:
    monkeypatch.setattr(base64, "b64decode", lambda *a, **k: pytest.fail("decoded"))
    with pytest.raises(TooLarge):
        decode_base64("QUFB" * 1_000_000, 1000)


@pytest.mark.parametrize("bad", ["not base64!", "QUFB=QUFB", "data:image/png,QUFB", "data:;base64", b"\xff\xfe"])
def test_invalid_base64_is_bad_input(bad) -> None:
    with pytest.raises(BadInput):
        decode_base64(bad, 1000)


def test_whitespace_is_ignored() -> None:
    assert decode_base64("QUJD\nREVG\n", 100) == b"ABCDEF"


# ── open_image ───────────────────────────────────────────────────────────────


def png(w: int, h: int, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h)).save(buf, format=fmt)
    return buf.getvalue()


def test_pixel_cap_is_checked_from_the_header() -> None:
    img = open_image(png(40, 30), max_pixels=1200)
    assert img.size == (40, 30)
    with pytest.raises(TooLarge, match="1199 pixels"):
        open_image(png(40, 30), max_pixels=1199)


def test_a_huge_declared_image_is_refused_before_decoding() -> None:
    """A PNG whose header claims 100k x 100k: refused from the header, no allocation."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    bomb = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 100_000, 100_000, 8, 2, 0, 0, 0))
    bomb += chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")
    with pytest.raises((TooLarge, BadInput)):
        open_image(bomb, max_pixels=40_000_000)


def test_formats_bytes_and_garbage() -> None:
    with pytest.raises(BadInput, match="JPEG"):
        open_image(png(4, 4), formats=("JPEG",))
    assert open_image(png(4, 4, "JPEG"), formats=("JPEG", "PNG")).format == "JPEG"
    with pytest.raises(TooLarge):
        open_image(png(4, 4), max_bytes=10)
    with pytest.raises(BadInput):
        open_image(b"not an image")


def test_a_non_ascii_digit_length_is_bad_input() -> None:
    with pytest.raises(BadInput):
        run(read_body(FakeRequest([b"x"], content_length="²"), 10))


def test_oversized_base64_bytes_are_refused_before_decoding() -> None:
    with pytest.raises(TooLarge):
        decode_base64(b"\xff" * 100_000, 10)


def test_a_decompression_bomb_image_is_too_large_not_bad_input() -> None:
    buf = io.BytesIO()
    Image.new("1", (20_000, 10_000)).save(buf, "PNG")  # 200M pixels: over 2x Pillow's MAX_IMAGE_PIXELS
    with pytest.raises(TooLarge):
        open_image(buf.getvalue(), max_pixels=10**12)
