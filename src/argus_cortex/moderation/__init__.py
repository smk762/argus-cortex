"""Content-moderation clients the stages share: NudeNet, SingGuard and OCR over HTTP.

Each client talks to a server the deployer runs; nothing here imports a
detector. OCR (argus-retina's text reader) is a sensor rather than a moderator,
but it shares NudeNet's wire shape and this package's HTTP path. Stdlib + pydantic only (Pillow, for SingGuard's downscaling, is
behind the ``[images]`` extra and imported only when used). Configuration is
the caller's: each stage reads its own environment and passes a URL in.
"""

from __future__ import annotations

from argus_cortex.moderation._http import CheckFailed, ModerationConfigError
from argus_cortex.moderation.nudenet import (
    CONTENT_RATINGS,
    DEFAULT_EXPOSED_LABELS,
    NUDENET_LABELS,
    SUGGESTIVE_LABELS,
    ContentRating,
    NsfwDetection,
    NsfwResult,
    NudeNetCheckFailed,
    NudeNetClient,
    NudeNetInfo,
    NudeNetLabel,
    exposed_score,
    rate_content,
)
from argus_cortex.moderation.ocr import (
    OcrCheckFailed,
    OcrClient,
    OcrInfo,
    OcrLine,
    OcrResult,
    normalize_text,
)
from argus_cortex.moderation.singguard import (
    INPUT_ROLES,
    THINKING_MODES,
    GuardCheckFailed,
    GuardResult,
    SingGuardClient,
    policy_name,
    rule_letter,
    rule_matches,
)

__all__ = [
    "CONTENT_RATINGS",
    "DEFAULT_EXPOSED_LABELS",
    "INPUT_ROLES",
    "NUDENET_LABELS",
    "SUGGESTIVE_LABELS",
    "THINKING_MODES",
    "CheckFailed",
    "ContentRating",
    "GuardCheckFailed",
    "GuardResult",
    "ModerationConfigError",
    "NsfwDetection",
    "NsfwResult",
    "NudeNetCheckFailed",
    "NudeNetClient",
    "NudeNetInfo",
    "NudeNetLabel",
    "OcrCheckFailed",
    "OcrClient",
    "OcrInfo",
    "OcrLine",
    "OcrResult",
    "SingGuardClient",
    "exposed_score",
    "normalize_text",
    "policy_name",
    "rate_content",
    "rule_letter",
    "rule_matches",
]
