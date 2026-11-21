"""OCR client: the text in an image, read by argus-retina's OCR server over HTTP.

The server is ``ghcr.io/smk762/argus-retina-ocr`` (RapidOCR, PP-OCRv6 small; argus-retina#5). It reads what is
printed and decides nothing: whether a caption's claim holds is the caller's call. The intended use is argus-lens
reconcile dropping a name a caption says it read when the image doesn't carry it.

API: NudeNet's ``POST /infer`` (one image as multipart field ``f1``), answered by
``{"prediction": [[{"class": "text", "score", "box": [x, y, w, h], "text"}, ...]], "success": true}``: one detection
per line of text, in reading order (rows top to bottom, then left to right; two facing pages interleave). ``score``
is the recogniser's confidence in the line, and the server drops lines under its ``min_score`` (0.5 by default).
``GET /health`` names the models and that floor (:class:`OcrInfo`).

Words sometimes run together ("afProfessor"), so compare with :func:`normalize_text` on both sides, as
:meth:`OcrResult.contains` does, never with a plain substring test.
"""

from __future__ import annotations

import unicodedata

from pydantic import BaseModel, Field

from argus_cortex.moderation._http import (
    CheckFailed,
    get_json,
    is_number,
    multipart_image,
    post_json,
    service_url,
    sibling_url,
)


class OcrLine(BaseModel):
    """One line of text the server read."""

    text: str
    score: float = Field(..., ge=0.0, le=1.0, description="The recogniser's confidence in the line.")
    bbox: list[float] = Field(..., min_length=4, max_length=4, description="[x, y, w, h]")


class OcrResult(BaseModel):
    """The text in one image, line by line in reading order. No lines: the server read nothing."""

    lines: list[OcrLine] = Field(default_factory=list)

    def transcript(self, min_score: float = 0.0) -> str:
        """The lines scoring at least *min_score*, one per row of the string, in reading order."""
        return "\n".join(line.text for line in self.lines if line.score >= min_score)

    def contains(self, phrase: str, min_score: float = 0.0) -> bool:
        """Whether *phrase* appears in the lines scoring at least *min_score*, compared by :func:`normalize_text`.

        Lines are joined before matching, so a name broken across two lines still matches. A phrase with nothing
        left after normalising (blank, or only punctuation) never matches: there is nothing to have read.
        """
        wanted = normalize_text(phrase)
        return bool(wanted) and wanted in normalize_text(self.transcript(min_score))


class OcrInfo(BaseModel):
    """What an OCR server says it runs (its ``GET /health``). ``None``: the server did not say."""

    model: str | None = Field(None, description="The engine and models, e.g. 'PP-OCRv6 small (RapidOCR)'.")
    det: str | None = Field(None, description="The text detector's model file.")
    rec: str | None = Field(None, description="The recogniser's model file.")
    min_score: float | None = Field(None, description="Lines the server read with less confidence are dropped.")


class OcrCheckFailed(CheckFailed):
    """One image could not be read by the OCR server."""


_MARKS = frozenset({"Mn", "Mc"})  # combining marks: part of the letter they follow


def normalize_text(text: str) -> str:
    """*text* reduced for comparison: NFKC, case-folded, letters and digits with their combining marks.

    Spaces and punctuation go, because OCR runs words together and drops or adds full stops ("N.Y.Public").
    Accents stay: "Espinàs" and "Espinas" differ, and deciding they match is the caller's policy. That covers
    marks with no precomposed letter too (Devanagari vowel signs and viramas, Thai and Arabic marks), which are
    not alphanumeric on their own: without them "हिन्दी" would be its bare consonants.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in folded if c.isalnum() or unicodedata.category(c) in _MARKS)


def to_result(raw: list) -> OcrResult:
    """One image's detections as lines, in the server's (reading) order.

    An entry that isn't a well-formed text line is skipped: a malformed entry is no reading at all.
    """
    lines: list[OcrLine] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        cls, text, score, box = d.get("class"), d.get("text"), d.get("score"), d.get("box")
        if (
            cls != "text"
            or not isinstance(text, str)
            or not text.strip()
            or not is_number(score)
            or not 0.0 <= score <= 1.0
            or not isinstance(box, list)
            or len(box) != 4
            or not all(is_number(v) for v in box)
        ):
            continue
        lines.append(OcrLine(text=text, score=round(float(score), 4), bbox=[round(float(v), 1) for v in box]))
    return OcrResult(lines=lines)


class OcrClient:
    """An OCR server at *base_url* (its origin, e.g. ``http://ocr:8133``).

    Thread-safe: :meth:`read` holds no state, so one client can serve a pool.
    """

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.url = service_url(base_url, "/infer", "OCR")
        self.health_url = sibling_url(self.url, "/infer", "/health")
        self.timeout = timeout

    def info(self) -> OcrInfo:
        """The server's models and score floor, from ``GET /health``; an unreachable server raises
        :class:`OcrCheckFailed`."""
        payload = get_json(self.health_url, self.timeout, "OCR", OcrCheckFailed, missing_ok=True)
        if not isinstance(payload, dict):
            return OcrInfo()

        def name(key: str) -> str | None:
            value = payload.get(key)
            return value if isinstance(value, str) and value else None

        min_score = payload.get("min_score")
        return OcrInfo(
            model=name("model"),
            det=name("det"),
            rec=name("rec"),
            min_score=float(min_score) if is_number(min_score) else None,
        )

    def read(self, data: bytes, filename: str = "image") -> OcrResult:
        """Post one image and parse the lines; raises :class:`OcrCheckFailed`.

        A failed read raises rather than returning no lines, so a caller never drops a claim because the server
        was down.
        """
        body, content_type = multipart_image(data, filename)
        payload = post_json(self.url, body, content_type, self.timeout, "OCR", OcrCheckFailed)
        prediction = payload.get("prediction") if isinstance(payload, dict) else None
        if not (isinstance(payload, dict) and payload.get("success") is True and isinstance(prediction, list)):
            raise OcrCheckFailed("OCR did not report success")
        if len(prediction) != 1 or not isinstance(prediction[0], list):
            raise OcrCheckFailed(f"OCR returned {len(prediction)} results for one image")
        return to_result(prediction[0])
