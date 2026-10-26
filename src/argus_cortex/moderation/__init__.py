"""Content-moderation clients the stages share: NudeNet and SingGuard over HTTP.

Each client talks to a server the deployer runs; nothing here imports a
detector. Stdlib + pydantic only (Pillow, for SingGuard's downscaling, is
behind the ``[images]`` extra and imported only when used). Configuration is
the caller's: each stage reads its own environment and passes a URL in.
"""

from __future__ import annotations

from argus_cortex.moderation._http import CheckFailed, ModerationConfigError
from argus_cortex.moderation.nudenet import (
    DEFAULT_EXPOSED_LABELS,
    NUDENET_LABELS,
    NsfwDetection,
    NsfwResult,
    NudeNetCheckFailed,
    NudeNetClient,
    NudeNetLabel,
    exposed_score,
)
from argus_cortex.moderation.singguard import (
    THINKING_MODES,
    GuardCheckFailed,
    GuardResult,
    SingGuardClient,
    policy_name,
    rule_letter,
    rule_matches,
)

__all__ = [
    "DEFAULT_EXPOSED_LABELS",
    "NUDENET_LABELS",
    "THINKING_MODES",
    "CheckFailed",
    "GuardCheckFailed",
    "GuardResult",
    "ModerationConfigError",
    "NsfwDetection",
    "NsfwResult",
    "NudeNetCheckFailed",
    "NudeNetClient",
    "NudeNetLabel",
    "SingGuardClient",
    "exposed_score",
    "policy_name",
    "rule_letter",
    "rule_matches",
]
