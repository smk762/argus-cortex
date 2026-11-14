from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from argus_cortex.provenance import CLEARED_KINDS, PROVENANCE_KINDS, Provenance, provenance_schema

SHA = "a" * 64


def test_personal_needs_only_a_source_and_is_never_cleared() -> None:
    p = Provenance(kind="personal", source="my own photo")
    assert p.license == ""
    assert p.cleared is False


@pytest.mark.parametrize("kind", ["stock", "synthetic"])
def test_stock_and_synthetic_are_cleared_with_a_licence(kind: str) -> None:
    assert Provenance(kind=kind, source="x", license="CC0-1.0").cleared is True


@pytest.mark.parametrize("kind", ["stock", "synthetic", "consented", "curated"])
def test_every_kind_but_personal_states_its_licence(kind: str) -> None:
    with pytest.raises(ValidationError, match="licence"):
        Provenance(kind=kind, source="x", subject="Jo", release_sha256=SHA, license="  ")


def test_consented_needs_subject_and_release_hash() -> None:
    p = Provenance(kind="consented", source="studio session", license="release", subject="Jo", release_sha256=SHA)
    assert p.cleared is True
    with pytest.raises(ValidationError, match="subject"):
        Provenance(kind="consented", source="x", license="release", release_sha256=SHA)
    with pytest.raises(ValidationError, match="subject"):
        Provenance(kind="consented", source="x", license="release", subject="Jo")


def test_release_hash_is_a_lowercase_sha256() -> None:
    with pytest.raises(ValidationError):
        Provenance(kind="consented", source="x", license="release", subject="Jo", release_sha256="A" * 64)


def test_curated_is_reserved_and_not_cleared() -> None:
    assert Provenance(kind="curated", source="x", license="CC0-1.0").cleared is False


def test_rejects_unknown_kind_and_empty_source() -> None:
    with pytest.raises(ValidationError):
        Provenance(kind="scraped", source="x", license="?")
    with pytest.raises(ValidationError):
        Provenance(kind="personal", source="")


def test_a_blank_source_is_refused() -> None:
    with pytest.raises(ValidationError, match="came from"):
        Provenance(kind="personal", source="   ")


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError, match="liscense"):
        Provenance.model_validate({"kind": "stock", "source": "x", "license": "CC0-1.0", "liscense": "CC0-1.0"})


def test_licence_is_read_as_license() -> None:
    assert Provenance(kind="stock", source="x", licence="CC0-1.0").license == "CC0-1.0"
    assert Provenance(kind="personal", source="x", licence="CC0-1.0").license == "CC0-1.0"
    same = {"kind": "stock", "source": "x", "license": "CC0-1.0", "licence": "CC0-1.0"}
    assert Provenance.model_validate(same).license == "CC0-1.0"
    with pytest.raises(ValidationError, match="disagree"):
        Provenance.model_validate({**same, "licence": "CC-BY-4.0"})


def test_a_stored_cleared_flag_is_recomputed_not_believed() -> None:
    data = {"kind": "personal", "source": "x", "cleared": True}
    assert Provenance.model_validate(data).cleared is False


def test_round_trips_with_cleared_in_the_payload() -> None:
    p = Provenance(kind="stock", source="unsplash", license="Unsplash")
    data = json.loads(p.model_dump_json())
    assert data["cleared"] is True
    # A reader validating the dumped payload accepts the computed field back.
    assert Provenance.model_validate(data) == p


def test_is_frozen_so_a_record_cannot_skip_its_check() -> None:
    p = Provenance(kind="personal", source="my own photo")
    with pytest.raises(ValidationError):
        p.kind = "consented"


def test_kinds_match_literal() -> None:
    assert set(PROVENANCE_KINDS) == {"personal", "stock", "synthetic", "consented", "curated"}
    assert set(CLEARED_KINDS) == {"stock", "synthetic", "consented"}


SCHEMA_FILE = Path(__file__).resolve().parent.parent / "provenance-schema.json"


def test_committed_schema_matches_provenance() -> None:
    assert SCHEMA_FILE.read_text() == provenance_schema(), "provenance-schema.json is stale; run `make schema`"


def test_schema_lists_every_kind_and_marks_cleared_read_only() -> None:
    props = json.loads(provenance_schema())["$defs"]["Provenance"]["properties"]
    assert props["kind"]["enum"] == list(PROVENANCE_KINDS)
    assert props["cleared"]["readOnly"] is True


GEN = {"model": "flux1-dev", "model_sha256": "e" * 64, "seed": 42, "prompt": "a portrait, studio light"}


@pytest.mark.parametrize("kind", ["synthetic", "personal"])
def test_a_generated_source_may_name_its_generator(kind: str) -> None:
    p = Provenance(kind=kind, source="darkroom", license="CC0-1.0", generator=GEN)
    assert p.generator is not None and p.generator.seed == 42
    assert Provenance.model_validate(json.loads(p.model_dump_json())) == p


def test_generator_is_optional_and_needs_only_a_model() -> None:
    assert Provenance(kind="synthetic", source="x", license="CC0-1.0").generator is None
    p = Provenance(kind="synthetic", source="x", license="CC0-1.0", generator={"model": "sdxl"})
    assert p.generator is not None and (p.generator.model_sha256, p.generator.seed, p.generator.prompt) == (
        None,
        None,
        "",
    )


@pytest.mark.parametrize("kind", ["stock", "consented", "curated"])
def test_only_synthetic_and_personal_carry_a_generator(kind: str) -> None:
    with pytest.raises(ValidationError, match="no generator"):
        Provenance(kind=kind, source="x", license="L", subject="Jo", release_sha256=SHA, generator=GEN)


@pytest.mark.parametrize(
    "gen",
    [{"model": "  "}, {"model": "m", "model_sha256": "nope"}, {"model": "m", "sampler": "euler"}, {"prompt": "p"}],
)
def test_a_malformed_generator_is_refused(gen: dict) -> None:
    with pytest.raises(ValidationError):
        Provenance(kind="synthetic", source="x", license="CC0-1.0", generator=gen)
