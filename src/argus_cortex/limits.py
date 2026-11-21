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
* :func:`open_image`: checks the pixel count from the header before anything is decoded, and only
  tries the formats it is given (:data:`DEFAULT_FORMATS`, JPEG/PNG/WEBP, unless the caller opts in to
  more), never EPS, which Pillow hands to Ghostscript. Pillow's own ``MAX_IMAGE_PIXELS`` is a warning
  threshold, not a limit.
* :func:`open_audio`: checks the format (from the magic bytes), channels, sample rate and duration from
  the header before any sample is decoded, then decodes at most the duration cap, whatever the header
  claimed. WAV and FLAC only.

Over a cap raises :class:`TooLarge` (``413`` at a server's edge); malformed input raises
:class:`BadInput` (``400``). Both are ``ValueError``. Design: ``docs/paved-roads.md``.
``read_body``/``read_upload`` take Starlette objects (``[server]``); ``open_image`` needs Pillow
(``[images]``); ``open_audio`` needs soundfile and numpy (``[audio]``); ``decode_base64`` is stdlib.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import io
import threading
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    import numpy as np
    from PIL import Image

__all__ = [
    "AUDIO_FORMATS",
    "CHUNK",
    "DEFAULT_FORMATS",
    "DEFAULT_SAMPLE_RATES",
    "Audio",
    "BadInput",
    "TooLarge",
    "decode_base64",
    "open_audio",
    "open_image",
    "read_body",
    "read_upload",
]

CHUNK = 1024 * 1024
DEFAULT_FORMATS: tuple[str, ...] = ("JPEG", "PNG", "WEBP")
"""The Pillow formats :func:`open_image` tries when the caller names none."""
_REFUSED_FORMATS = frozenset({"EPS"})  # Pillow decodes EPS/PS by running Ghostscript on the bytes


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
    if len(raw) > 19:  # past any real body size, and int() refuses > 4300 digits (ValueError, not BadInput)
        raise BadInput("Content-Length is too long")
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


_PILLOW_LIMIT_LOCK = threading.Lock()


@contextlib.contextmanager
def _pillow_limit_at_least(image_module: Any, max_pixels: int) -> Iterator[None]:
    """Keep Pillow's process-wide bomb check from refusing an image the caller's *max_pixels* allows.

    ``Image.open`` raises past 2 x ``Image.MAX_IMAGE_PIXELS`` (about 179M by default) whatever the caller
    asked for. Only when *max_pixels* is above that is the global raised, to *max_pixels*, for this one
    header read, under a lock; ``open_image`` then applies *max_pixels* itself. The usual caps never get here.
    """
    current = image_module.MAX_IMAGE_PIXELS
    if current is None or max_pixels <= 2 * current:
        yield
        return
    with _PILLOW_LIMIT_LOCK:
        current = image_module.MAX_IMAGE_PIXELS
        if current is not None and max_pixels > 2 * current:
            image_module.MAX_IMAGE_PIXELS = max_pixels
        try:
            yield
        finally:
            image_module.MAX_IMAGE_PIXELS = current


def open_image(
    data: bytes, *, max_pixels: int = 40_000_000, max_bytes: int | None = None, formats: tuple[str, ...] | None = None
) -> Image.Image:
    """Open *data* as an image without decoding it, after checking its pixel count (from the
    header) and, if given, its byte size.

    Only the Pillow plugins in *formats* see the bytes: :data:`DEFAULT_FORMATS` (JPEG, PNG, WEBP)
    when it is ``None`` or empty. Pass more (``("JPEG", "PNG", "WEBP", "GIF")``) to opt in to them. EPS is
    refused even when asked for, since Pillow decodes it with Ghostscript.

    Returns the lazily-opened image; decoding happens on ``.load()`` / use, after the checks."""
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover
        raise ImportError("open_image requires Pillow: pip install argus-cortex[images]") from exc

    allowed = [f.upper() for f in (formats or DEFAULT_FORMATS)]
    if refused := _REFUSED_FORMATS.intersection(allowed):
        raise ValueError(f"open_image never opens {', '.join(sorted(refused))} (untrusted bytes reach Ghostscript)")
    Image.init()
    if unknown := [f for f in allowed if f not in Image.OPEN]:  # Pillow raises KeyError on these, mid-open
        raise ValueError(f"open_image: Pillow has no reader for {', '.join(unknown)}")
    if max_bytes is not None and len(data) > max_bytes:
        raise TooLarge("image", max_bytes)
    try:
        with _pillow_limit_at_least(Image, max_pixels):
            img = Image.open(io.BytesIO(data), formats=allowed)
    except Image.DecompressionBombError as exc:  # over Pillow's own limit, so over max_pixels: not malformed
        raise TooLarge("image", max_pixels, "pixels") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise BadInput(f"not an image in {', '.join(allowed)}") from exc
    w, h = img.size
    if w <= 0 or h <= 0:
        raise BadInput("image has no pixels")
    if w * h > max_pixels:
        raise TooLarge("image", max_pixels, "pixels")
    return img


AUDIO_FORMATS: tuple[str, ...] = ("WAV", "FLAC")
"""The containers :func:`open_audio` reads. Only their magic bytes reach libsndfile."""
DEFAULT_SAMPLE_RATES: tuple[int, ...] = (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000)
"""The sample rates :func:`open_audio` accepts when the caller names none."""


class Audio(NamedTuple):
    """A decoded clip: float32 *samples* (integer PCM scaled to [-1, 1); a float file keeps its values,
    so clipping past full scale stays visible), shaped ``(frames, channels)``, or ``(frames,)``
    when downmixed to mono, and its *sample_rate* in Hz."""

    samples: np.ndarray
    sample_rate: int

    @property
    def duration(self) -> float:
        """The clip's length in seconds."""
        return len(self.samples) / self.sample_rate


def _audio_format(data: bytes) -> str | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "WAV"
    if data[:4] == b"fLaC":
        return "FLAC"
    return None


def open_audio(
    data: bytes,
    *,
    max_seconds: float = 60.0,
    max_channels: int = 2,
    allowed_rates: tuple[int, ...] | None = None,
    max_bytes: int | None = None,
    mono: bool = False,
) -> Audio:
    """Decode a WAV or FLAC clip after checking its byte size (if given), then its channel count, sample
    rate and duration from the header, before any sample is decoded.

    The header is not trusted for the length: decoding stops one frame past *max_seconds*, so a header
    that claims a short clip over a long stream still costs no more than the cap, and a stream longer
    than the cap is refused. *allowed_rates* defaults to :data:`DEFAULT_SAMPLE_RATES`, which bounds the
    frames *max_seconds* allows. *mono* averages the channels.

    :class:`TooLarge` for a clip over a cap, :class:`BadInput` for anything that isn't a readable WAV or
    FLAC clip or whose rate or channel count isn't allowed (a format question, not a size one)."""
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as exc:  # pragma: no cover
        raise ImportError("open_audio requires soundfile and numpy: pip install argus-cortex[audio]") from exc

    rates = allowed_rates or DEFAULT_SAMPLE_RATES
    if max_seconds <= 0 or max_channels < 1:
        raise ValueError("open_audio: max_seconds must be positive and max_channels at least 1")
    if max_bytes is not None and len(data) > max_bytes:
        raise TooLarge("audio", max_bytes)
    fmt = _audio_format(data)
    if fmt is None:  # sniffed here so libsndfile's other parsers (MP3, OGG, MAT...) never see the bytes
        raise BadInput(f"not audio in {', '.join(AUDIO_FORMATS)}")
    try:
        with sf.SoundFile(io.BytesIO(data)) as clip:
            if clip.format not in (fmt, "WAVEX" if fmt == "WAV" else fmt):
                raise BadInput(f"not audio in {', '.join(AUDIO_FORMATS)}")
            if clip.channels < 1 or clip.samplerate < 1:
                raise BadInput("audio has no channels or no sample rate")
            if clip.channels > max_channels:
                raise BadInput(f"audio has {clip.channels} channels; at most {max_channels} are allowed")
            if clip.samplerate not in rates:
                raise BadInput(f"audio sample rate {clip.samplerate} Hz is not one of {', '.join(map(str, rates))}")
            cap = int(max_seconds * clip.samplerate)
            if clip.frames > cap:
                raise TooLarge(f"audio at {clip.samplerate} Hz", cap, "frames")
            samples = clip.read(frames=cap + 1, dtype="float32", always_2d=True)
            rate = clip.samplerate
    except (BadInput, TooLarge):  # both are ValueError
        raise
    except (sf.LibsndfileError, RuntimeError, OSError, ValueError, TypeError, EOFError) as exc:
        raise BadInput(f"not readable audio in {', '.join(AUDIO_FORMATS)}") from exc
    if len(samples) > cap:
        raise TooLarge(f"audio at {rate} Hz", cap, "frames")
    if len(samples) == 0:
        raise BadInput("audio has no samples")
    if not np.isfinite(samples).all():
        raise BadInput("audio has non-finite samples")
    if mono:
        samples = samples.mean(axis=1, dtype=np.float32)
    return Audio(samples, rate)
