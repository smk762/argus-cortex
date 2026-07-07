from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus_core.taxonomy import TARGET_CATEGORIES, TARGET_STYLES, TargetProfile


def test_defaults() -> None:
    p = TargetProfile()
    assert p.target_style == "photo"
    assert p.target_category == "identity"
    assert p.target_backend == "sdxl"
    assert p.checkpoint is None


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
    assert set(TARGET_CATEGORIES) == {"identity", "wardrobe", "pose_composition", "setting"}
