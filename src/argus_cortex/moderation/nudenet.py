"""NudeNet client: nudity detection through a NudeNet server over HTTP.

The suite never imports NudeNet (AGPL-3.0): it posts images to a NudeNet
server (``ghcr.io/notai-tech/nudenet`` or any server speaking the same API)
and records the detections. Running that server is the deployer's choice;
cortex and the stages stay MIT.

This module is also the suite's one definition of what counts as nudity:
:data:`DEFAULT_EXPOSED_LABELS`. Faces, covered body parts, and exposed feet,
belly, armpits or male chest are detected too but do not count towards
:attr:`NsfwResult.exposed_score`.

API: ``POST /infer`` with each image as a multipart field ``f1``, ``f2``, …,
answered by ``{"prediction": [[{"class", "score", "box"}, ...], ...],
"success": true}`` in field order. ``box`` is ``[x, y, w, h]`` in source
pixels, and the server drops detections scoring under 0.2.

Note: the upstream ``ghcr.io/notai-tech/nudenet`` image reports ``y`` offset by
the letterbox padding on non-square images (scores are unaffected).
"""

from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from argus_cortex.moderation._http import CheckFailed, post_json, service_url

# NudeNet's 18 detector classes, verbatim (they are its API vocabulary).
NudeNetLabel = Literal[
    "FEMALE_GENITALIA_COVERED",
    "FACE_FEMALE",
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
    "FEET_EXPOSED",
    "BELLY_COVERED",
    "FEET_COVERED",
    "ARMPITS_COVERED",
    "ARMPITS_EXPOSED",
    "FACE_MALE",
    "BELLY_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_COVERED",
    "FEMALE_BREAST_COVERED",
    "BUTTOCKS_COVERED",
]
NUDENET_LABELS: tuple[NudeNetLabel, ...] = NudeNetLabel.__args__
# The classes an image's exposed score (its "total" NSFW score) is taken over.
DEFAULT_EXPOSED_LABELS: tuple[NudeNetLabel, ...] = (
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
)
# NudeNet servers report nothing scoring under this.
MIN_REPORTED_SCORE = 0.2


class NsfwDetection(BaseModel):
    """One NudeNet detection."""

    label: NudeNetLabel
    score: float
    bbox: list[float] = Field(..., min_length=4, max_length=4, description="[x, y, w, h]")


class NsfwResult(BaseModel):
    """NudeNet's view of one image.

    The server reports only detections scoring >= 0.2, so a class missing from
    ``labels`` scored below that. ``exposed_score`` is the highest score among
    :data:`DEFAULT_EXPOSED_LABELS`; filters can recompute it over another set.
    """

    detections: list[NsfwDetection] = Field(default_factory=list)
    # Highest score per class, highest first.
    labels: dict[NudeNetLabel, float] = Field(default_factory=dict)
    exposed_score: float = 0.0


class NudeNetCheckFailed(CheckFailed):
    """One image could not be checked by NudeNet."""


def exposed_score(labels: dict[str, float], exposed: tuple[str, ...] | list[str] = DEFAULT_EXPOSED_LABELS) -> float:
    """The highest score among the *exposed* classes (0.0 when none was detected)."""
    return max((labels[c] for c in exposed if c in labels), default=0.0)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def to_result(raw: list) -> NsfwResult:
    """One image's detections as recorded, plus the per-class maxima and exposed score."""
    known = set(NUDENET_LABELS)
    dets: list[NsfwDetection] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        label, score, box = d.get("class"), d.get("score"), d.get("box")
        if (
            label not in known
            or not _is_number(score)
            or not isinstance(box, list)
            or len(box) != 4
            or not all(_is_number(v) for v in box)
        ):
            continue  # an unknown class or malformed entry is not evidence either way
        dets.append(NsfwDetection(label=label, score=round(float(score), 4), bbox=[round(float(v), 1) for v in box]))
    dets.sort(key=lambda d: -d.score)
    labels: dict[str, float] = {}
    for d in dets:
        labels.setdefault(d.label, d.score)
    return NsfwResult(detections=dets, labels=labels, exposed_score=exposed_score(labels))


def _multipart(data: bytes, filename: str) -> tuple[bytes, str]:
    # The name lands in a header: keep it to characters that cannot break it.
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[-100:] or "image"
    boundary = uuid.uuid4().hex
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="f1"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    return head + data + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


class NudeNetClient:
    """A NudeNet server at *base_url* (its origin, e.g. ``http://nudenet:8080``).

    Thread-safe: :meth:`check` holds no state, so one client can serve a pool.
    """

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.url = service_url(base_url, "/infer", "NudeNet")
        self.timeout = timeout

    def check(self, data: bytes, filename: str = "image") -> NsfwResult:
        """Post one image and parse the answer; raises :class:`NudeNetCheckFailed`."""
        body, content_type = _multipart(data, filename)
        payload = post_json(self.url, body, content_type, self.timeout, "NudeNet", NudeNetCheckFailed)
        prediction = payload.get("prediction") if isinstance(payload, dict) else None
        if not (isinstance(payload, dict) and payload.get("success") is True and isinstance(prediction, list)):
            raise NudeNetCheckFailed("NudeNet did not report success")
        if len(prediction) != 1 or not isinstance(prediction[0], list):
            raise NudeNetCheckFailed(f"NudeNet returned {len(prediction)} results for one image")
        return to_result(prediction[0])
