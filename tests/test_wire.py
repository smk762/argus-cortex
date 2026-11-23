from __future__ import annotations

import pytest
from pydantic import BaseModel

from argus_cortex.wire import (
    VersionError,
    check_version,
    make_versioned_base,
    render_schema,
    schema_major,
    sync_schema,
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


def test_check_version_raises_custom_error_type() -> None:
    class MyError(RuntimeError):
        pass

    with pytest.raises(MyError, match="not supported"):
        check_version("2.0", ("1",), error=MyError)


def test_versioned_base_raises_custom_error_type() -> None:
    class MyError(RuntimeError):
        pass

    Base = make_versioned_base("proof_version", "1.0", ("1",), error=MyError)

    class Model(Base):  # type: ignore[misc, valid-type]
        x: int = 0

    with pytest.raises(MyError):
        Model.model_validate({"proof_version": "2.0"})


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


def test_sync_schema_writes_and_creates_the_directory(tmp_path) -> None:
    path = tmp_path / "schema" / "x-wire.schema.json"
    result = sync_schema("{}\n", path, command="argus-x schema")
    assert result.ok
    assert result.message == f"Wrote wire schema -> {path}"
    assert path.read_text(encoding="utf-8") == "{}\n"


def test_sync_schema_check_passes_on_the_committed_text_and_writes_nothing(tmp_path) -> None:
    path = tmp_path / "x.json"
    path.write_text("{}\n", encoding="utf-8")
    before = path.stat().st_mtime_ns
    assert sync_schema("{}\n", path, check=True, command="argus-x schema") == (True, f"{path} is up to date.")
    assert path.stat().st_mtime_ns == before


@pytest.mark.parametrize("committed", [None, "{}", '{"stale": true}\n'])
def test_sync_schema_check_fails_on_a_missing_or_different_file_and_leaves_it(tmp_path, committed) -> None:
    path = tmp_path / "x.json"
    if committed is not None:
        path.write_text(committed, encoding="utf-8")
    result = sync_schema("{}\n", path, check=True, command="argus-x schema")
    assert not result.ok
    assert result.message == f"{path} is stale: run `argus-x schema` and commit the result."
    assert (path.read_text(encoding="utf-8") if path.exists() else None) == committed


def test_sync_schema_round_trips_render_schema(tmp_path) -> None:
    class M(BaseModel):
        a: int

    path = tmp_path / "m.json"
    rendered = render_schema([M], title="t")
    sync_schema(rendered, path, command="c")
    assert sync_schema(render_schema([M], title="t"), path, check=True, command="c").ok
