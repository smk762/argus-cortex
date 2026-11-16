"""Client input can't exhaust memory (the suite's paved road for unbounded reads).

Read request bodies, uploads, base64 payloads and images through these instead of
``await request.body()``, ``await upload.read()``, ``base64.b64decode(...)`` or
``Image.open(...)``:

* :func:`read_body`: a declared ``Content-Length`` over the cap is refused before reading, a
  malformed one is refused, and a chunked body (no length) is counted as it streams.
* :func:`read_upload`: reads an ``UploadFile`` in chunks and stops at the cap (``upload.read()``
  reads everything).
* :func:`decode_base64`: checks the *encoded* length before decoding, then the decoded size.
  A ``data:...;base64,`` prefix is accepted.
* :func:`open_image`: checks the pixel count from the header before anything is decoded, and can
  restrict formats. Pillow's own ``MAX_IMAGE_PIXELS`` is a warning threshold, not a limit.

Over a cap raises :class:`TooLarge` (``413`` at a server's edge); malformed input raises
:class:`BadInput` (``400``). Both are ``ValueError``. Design: ``docs/paved-roads.md``.
``read_body``/``read_upload`` take Starlette objects (``[server]``); ``open_image`` needs Pillow
(``[images]``); ``decode_base64`` is stdlib.
"""

from __future__ import annotations

import base64
import binascii
import io
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from PIL import Image

__all__ = ["CHUNK", "BadInput", "TooLarge", "decode_base64", "open_image", "read_body", "read_upload"]

CHUNK = 1024 * 1024


class TooLarge(ValueError):
    """Client input is over its cap (map to 413)."""

    def __init__(self, what: str, limit: int, unit: str = "bytes") -> None:
        super().__init__(f"{what} is larger than {limit} {unit}")
        self.limit = limit


class BadInput(ValueError):
    """Client input is malformed: a bad Content-Length, invalid base64, not an image (map to 400)."""


def _declared(headers: Any) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    raw = raw.strip()
    if not (raw.isascii() and raw.isdigit()):  # "²" is isdigit() but int() refuses it
        raise BadInput("Content-Length is not a number")
    return int(raw)


async def read_body(request: Any, max_bytes: int, *, what: str = "request body") -> bytes:
    """The body of a Starlette *request*, at most *max_bytes*, else :class:`TooLarge`."""
    declared = _declared(request.headers)
    if declared is not None and declared > max_bytes:
        raise TooLarge(what, max_bytes)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            raise TooLarge(what, max_bytes)
    return bytes(body)


async def read_upload(upload: Any, max_bytes: int, *, what: str = "upload", chunk: int = CHUNK) -> bytes:
    """The contents of a Starlette/FastAPI ``UploadFile``, at most *max_bytes*, else :class:`TooLarge`.

    Reads at most *max_bytes* + 1 bytes, so an oversized upload costs no more than the cap."""
    size = getattr(upload, "size", None)
    if isinstance(size, int) and size > max_bytes:
        raise TooLarge(what, max_bytes)
    body = bytearray()
    while True:
        part = await upload.read(min(chunk, max_bytes + 1 - len(body)))
        if not part:
            return bytes(body)
        body += part
        if len(body) > max_bytes:
            raise TooLarge(what, max_bytes)


def _max_encoded(max_bytes: int) -> int:
    return 4 * ((max_bytes + 2) // 3)


def decode_base64(data: str | bytes, max_bytes: int, *, what: str = "base64 payload") -> bytes:
    """Decode standard base64 (an optional ``data:...;base64,`` prefix, whitespace ignored),
    checking the encoded length before decoding. :class:`TooLarge` or :class:`BadInput`."""
    if isinstance(data, bytes):
        if len(data) > 2 * _max_encoded(max_bytes) + 1024:  # before copying it into a str
            raise TooLarge(what, max_bytes)
        try:
            data = data.decode("ascii")
        except UnicodeDecodeError as exc:
            raise BadInput(f"{what} is not base64") from exc
    if data.startswith("data:"):
        head, sep, data = data.partition(",")
        if not sep or not head.endswith(";base64"):
            raise BadInput(f"{what} is not a base64 data URI")
    # Cheap first: a payload many times the cap is refused before even stripping whitespace.
    if len(data) > 2 * _max_encoded(max_bytes) + 1024:
        raise TooLarge(what, max_bytes)
    compact = "".join(data.split())
    if len(compact) > _max_encoded(max_bytes):
        raise TooLarge(what, max_bytes)
    try:
        out = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BadInput(f"{what} is not valid base64") from exc
    if len(out) > max_bytes:
        raise TooLarge(what, max_bytes)
    return out


def open_image(
    data: bytes, *, max_pixels: int = 40_000_000, max_bytes: int | None = None, formats: tuple[str, ...] | None = None
) -> Image.Image:
    """Open *data* as an image without decoding it, after checking its pixel count (from the
    header) and, if given, its byte size and format (``("JPEG", "PNG", "WEBP")``).

    Returns the lazily-opened image; decoding happens on ``.load()`` / use, after the checks."""
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover
        raise ImportError("open_image requires Pillow: pip install argus-cortex[images]") from exc

    if max_bytes is not None and len(data) > max_bytes:
        raise TooLarge("image", max_bytes)
    try:
        img = Image.open(io.BytesIO(data), formats=list(formats) if formats else None)
    except Image.DecompressionBombError as exc:  # over 2x Pillow's MAX_IMAGE_PIXELS: too large, not malformed
        raise TooLarge("image", max_pixels, "pixels") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise BadInput("not an image" + (f" in {', '.join(formats)}" if formats else "")) from exc
    w, h = img.size
    if w <= 0 or h <= 0:
        raise BadInput("image has no pixels")
    if w * h > max_pixels:
        raise TooLarge("image", max_pixels, "pixels")
    return img
