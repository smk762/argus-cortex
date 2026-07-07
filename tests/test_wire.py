from __future__ import annotations

import pytest
from pydantic import BaseModel

from argus_core.wire import (
    VersionError,
    check_version,
    make_versioned_base,
    render_schema,
    schema_major,
    wire_schema,
)


def test_schema_major() -> None:
    assert schema_major("1.0") == "1"
    assert schema_major("2.7") == "2"
    assert schema_major("10.3.1") == "10"


def test_check_version_accepts_supported_major() -> None:
    check_version("1.4", ("1",), label="proof_version")  # no raise


def test_check_version_refuses_unsupported_major() -> None:
    with pytest.raises(VersionError, match="proof_version 2.0 is not supported"):
        check_version("2.0", ("1",), label="proof_version")


def test_versioned_base_stamps_default() -> None:
    Base = make_versioned_base("proof_version", "1.0", ("1",), label="proof_version")

    class Model(Base):  # type: ignore[misc, valid-type]
        x: int = 0

    assert Model().proof_version == "1.0"
    assert Model(x=5).x == 5


def test_versioned_base_accepts_compatible_major() -> None:
    Base = make_versioned_base("proof_version", "1.0", ("1",))

    class Model(Base):  # type: ignore[misc, valid-type]
        x: int = 0

    assert Model.model_validate({"proof_version": "1.9", "x": 3}).x == 3


def test_versioned_base_refuses_incompatible_major() -> None:
    Base = make_versioned_base("proof_version", "1.0", ("1",))

    class Model(Base):  # type: ignore[misc, valid-type]
        x: int = 0

    with pytest.raises(VersionError):
        Model.model_validate({"proof_version": "2.0", "x": 3})


def test_versioned_base_supports_custom_field_name() -> None:
    Base = make_versioned_base("manifest_version", "2.0", ("1", "2"))

    class Row(Base):  # type: ignore[misc, valid-type]
        rel_path: str = ""

    assert Row().manifest_version == "2.0"
    Row.model_validate({"manifest_version": "1.3"})  # legacy major still supported
    with pytest.raises(VersionError):
        Row.model_validate({"manifest_version": "3.0"})


class _A(BaseModel):
    x: int
    y: str = "hi"


class _B(BaseModel):
    a: _A
    flag: bool = False


def test_wire_schema_has_defs() -> None:
    defs = wire_schema([_A, _B], title="test")["$defs"]
    assert "_A" in defs and "_B" in defs


def test_render_schema_is_canonical_and_deterministic() -> None:
    a = render_schema([_A, _B], title="test")
    b = render_schema([_A, _B], title="test")
    assert a == b
    assert a.endswith("\n")
    # sorted keys: within a properties block, keys are alphabetical
    assert a.index('"a"') < a.index('"flag"')
