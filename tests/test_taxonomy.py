from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from argus_cortex.taxonomy import (
    MODALITIES,
    SPEECH_CATEGORIES,
    TARGET_CATEGORIES,
    TARGET_STYLES,
    TargetProfile,
    taxonomy_schema,
)


def test_defaults() -> None:
    p = TargetProfile()
    assert p.target_style == "photo"
    assert p.target_category == "identity"
    assert p.target_backend == "sdxl"
    assert p.checkpoint is None
    assert p.modality == "image"


def test_accepts_valid_taxonomy() -> None:
    p = TargetProfile(target_style="anime", target_category="wardrobe", checkpoint="ckpt.safetensors")
    assert p.target_style == "anime"
    assert p.target_category == "wardrobe"


def test_rejects_invalid_style_and_category() -> None:
    with pytest.raises(ValidationError):
        TargetProfile(target_style="watercolor")
    with pytest.raises(ValidationError):
        TargetProfile(target_category="lighting")  # a caption bucket, not a target category


def test_round_trips() -> None:
    p = TargetProfile(target_style="anime", target_category="setting")
    assert TargetProfile.model_validate_json(p.model_dump_json()) == p


def test_tuples_match_literals() -> None:
    assert set(TARGET_STYLES) == {"photo", "anime"}
    assert set(TARGET_CATEGORIES) == {"identity", "wardrobe", "pose_composition", "setting", "concept"}


def test_a_profile_without_modality_reads_as_image_unchanged() -> None:
    """Every manifest written before speech existed carries no ``modality``."""
    old = {"target_style": "anime", "target_backend": "flux", "checkpoint": None, "target_category": "concept"}
    p = TargetProfile.model_validate(old)
    assert p.modality == "image"
    assert p.model_dump(exclude={"modality"}) == old


def test_speech_profile_validates_with_speech_defaults() -> None:
    p = TargetProfile(modality="speech")
    assert p.target_category == "voice"
    assert p.target_backend is None
    p = TargetProfile.model_validate({"modality": "speech", "target_category": "accent", "target_backend": "qwen3-tts"})
    assert (p.target_category, p.target_backend) == ("accent", "qwen3-tts")
    assert TargetProfile.model_validate_json(p.model_dump_json()) == p


@pytest.mark.parametrize("category", SPEECH_CATEGORIES)
def test_every_speech_category_validates(category: str) -> None:
    assert TargetProfile(modality="speech", target_category=category).target_category == category


def test_a_speech_profile_can_still_name_no_backend_or_an_explicit_one() -> None:
    assert TargetProfile(modality="speech", target_backend=None).target_backend is None
    assert TargetProfile(modality="speech", target_backend="xtts-v2").target_backend == "xtts-v2"


@pytest.mark.parametrize(
    "fields",
    [
        {"target_category": "voice"},  # an image profile never carries a speech category
        {"modality": "image", "target_category": "accent"},
        {"modality": "speech", "target_category": "identity"},  # nor a speech profile an image one
        {"modality": "video"},
    ],
)
def test_category_and_modality_must_match(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        TargetProfile(**fields)


def test_speech_tuples_match_literals() -> None:
    assert set(MODALITIES) == {"image", "speech"}
    assert set(SPEECH_CATEGORIES) == {"voice", "accent", "language", "speaking_style"}
    assert not set(SPEECH_CATEGORIES) & set(TARGET_CATEGORIES)


SCHEMA_FILE = Path(__file__).resolve().parent.parent / "taxonomy-schema.json"


def test_committed_schema_matches_taxonomy() -> None:
    assert SCHEMA_FILE.read_text() == taxonomy_schema(), "taxonomy-schema.json is stale; run `make schema`"


def test_schema_lists_every_literal() -> None:
    defs = json.loads(taxonomy_schema())["$defs"]["TargetProfile"]["properties"]
    categories = [v for branch in defs["target_category"]["anyOf"] for v in branch["enum"]]
    assert categories == list(TARGET_CATEGORIES) + list(SPEECH_CATEGORIES)
    assert defs["modality"]["enum"] == list(MODALITIES)
    assert defs["target_style"]["enum"] == list(TARGET_STYLES)
