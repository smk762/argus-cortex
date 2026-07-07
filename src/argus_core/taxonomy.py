"""The shared target taxonomy — what a dataset is curated *for*.

This is the "moat" the whole Argus suite pivots on: the curator scores and
labels exports against it, the captioner picks caption variants from it, the
forge renders trainer configs for it, and proof evaluates a LoRA against it. It
lived, copied verbatim, in argus-curator and argus-forge (whose comment noted it
should "eventually hoist into a shared argus-core package") — this is that hoist.

Keep this module dependency-light (pydantic only) and change it deliberately: a
change here ripples through every stage of the suite.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

TargetStyle = Literal["photo", "anime"]

# What the dataset is curated for. Note this is distinct from argus-lens's
# caption *buckets* (identity/action/pose_composition/wardrobe/lighting/setting):
# those describe caption fragments, this describes the training objective.
TargetCategory = Literal["identity", "wardrobe", "pose_composition", "setting"]

TARGET_STYLES: tuple[TargetStyle, ...] = ("photo", "anime")
TARGET_CATEGORIES: tuple[TargetCategory, ...] = ("identity", "wardrobe", "pose_composition", "setting")


class TargetProfile(BaseModel):
    """What a dataset was curated for — shared verbatim across the suite.

    Inherited unchanged from stage to stage (curator → lens → forge → proof) so
    a decision made once at curation time is honoured everywhere downstream; no
    stage remaps the taxonomy.
    """

    target_style: TargetStyle = "photo"
    target_backend: str | None = "sdxl"
    checkpoint: str | None = None
    target_category: TargetCategory = "identity"
