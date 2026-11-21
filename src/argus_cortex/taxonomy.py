"""The shared target taxonomy — what a dataset is curated *for*.

This is the "moat" the whole Argus suite pivots on: the curator scores and
labels exports against it, the captioner picks caption variants from it, the
forge renders trainer configs for it, and proof evaluates a LoRA against it. It
lived, copied verbatim, in argus-curator and argus-forge (whose comment noted it
should "eventually hoist into a shared argus-cortex package") — this is that hoist.

Keep this module dependency-light (pydantic only) and change it deliberately: a
change here ripples through every stage of the suite.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, model_validator

from argus_cortex.wire import render_schema

TargetStyle = Literal["photo", "anime"]

# What kind of data the dataset holds. Every profile written before speech
# existed has no ``modality`` and reads as ``image``.
Modality = Literal["image", "speech"]

# What the dataset is curated for. Note this is distinct from argus-lens's
# caption *buckets* (identity/action/pose_composition/wardrobe/lighting/setting):
# those describe caption fragments, this describes the training objective.
# ``concept`` is a style or visual-concept LoRA: what argus-quarry acquires
# under its ``concept`` subjects.
TargetCategory = Literal["identity", "wardrobe", "pose_composition", "setting", "concept"]

# What a speech dataset is curated for, kept apart from ``TargetCategory`` so
# image code (forge's ``dict[TargetCategory, ...]`` tables, curator's face
# rules) never meets a speech value:
# ``voice``: one speaker, a fine-tuned or adapted voice.
# ``accent``: a regional accent across many speakers (the zero-shot case: the
#   speaker comes from a reference clip, the accent from training).
# ``language``: a new language or variety.
# ``speaking_style``: spontaneous, emotional or read speech.
SpeechCategory = Literal["voice", "accent", "language", "speaking_style"]

TARGET_STYLES: tuple[TargetStyle, ...] = ("photo", "anime")
TARGET_CATEGORIES: tuple[TargetCategory, ...] = ("identity", "wardrobe", "pose_composition", "setting", "concept")
MODALITIES: tuple[Modality, ...] = ("image", "speech")
SPEECH_CATEGORIES: tuple[SpeechCategory, ...] = ("voice", "accent", "language", "speaking_style")

# The defaults a speech profile gets for fields whose schema default is an image one.
_SPEECH_DEFAULTS: dict[str, Any] = {"target_category": "voice", "target_backend": None}


class TargetProfile(BaseModel):
    """What a dataset was curated for — shared verbatim across the suite.

    Inherited unchanged from stage to stage (curator → lens → forge → proof) so
    a decision made once at curation time is honoured everywhere downstream; no
    stage remaps the taxonomy.

    ``modality`` defaults to ``image``, so a profile without it reads exactly as
    before. A ``speech`` profile pairs with a :data:`SpeechCategory` (default
    ``voice``) and an image profile with a :data:`TargetCategory`; a mismatch is
    refused. For speech, ``target_backend`` names a TTS model family
    (``qwen3-tts``, ``xtts-v2``) and defaults to ``None`` rather than ``sdxl``,
    and ``target_style`` is ignored. Per-clip language and accent labels belong
    in the stage's manifest rows, not here.
    """

    modality: Modality = "image"
    target_style: TargetStyle = "photo"
    target_backend: str | None = "sdxl"
    checkpoint: str | None = None
    target_category: TargetCategory | SpeechCategory = "identity"

    @model_validator(mode="before")
    @classmethod
    def _speech_defaults(cls, data: Any) -> Any:
        """A speech profile that omits a field whose default is visual gets the speech default instead."""
        if isinstance(data, dict) and data.get("modality") == "speech":
            data = {**_SPEECH_DEFAULTS, **data}
        return data

    @model_validator(mode="after")
    def _category_matches_modality(self) -> TargetProfile:
        speech = self.target_category in SPEECH_CATEGORIES
        if speech != (self.modality == "speech"):
            allowed = SPEECH_CATEGORIES if self.modality == "speech" else TARGET_CATEGORIES
            raise ValueError(
                f"target_category {self.target_category!r} is not a {self.modality} category "
                f"(one of {', '.join(allowed)})"
            )
        return self


TAXONOMY_SCHEMA_TITLE = "argus-cortex taxonomy"


def taxonomy_schema() -> str:
    """The canonical JSON Schema for the taxonomy, as committed in ``taxonomy-schema.json``.

    Non-Python clients (argus-studio) generate their types from that file instead
    of hand-copying the literals. ``tests/test_taxonomy.py`` fails if the
    committed file and this module drift; regenerate it with ``make schema``.
    """
    return render_schema([TargetProfile], title=TAXONOMY_SCHEMA_TITLE)
