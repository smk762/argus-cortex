"""Versioned wire-schema tooling shared across the suite.

Every Argus service defines a wire contract (Pydantic models exchanged over
HTTP/CLI and codegen'd against by argus-studio) and stamps it with a version so
a consumer can refuse an incompatible major instead of misreading it — the
``MANIFEST_VERSION`` / ``PROOF_VERSION`` discipline. That machinery was
re-implemented per repo; this module is the single copy.

A package supplies its own version constant and field name (``proof_version``,
``manifest_version`` — kept per-package for readable payloads) and gets:

* :func:`check_version` — the major-compatibility gate;
* :func:`make_versioned_base` — a Pydantic base that stamps + checks the field;
* :func:`render_schema` — the canonical committed-schema string, so the
  ``schema`` / ``schema --check`` CLI is identical everywhere.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, model_validator

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


class VersionError(RuntimeError):
    """A wire payload's version is incompatible with this build."""


def schema_major(version: str) -> str:
    """The major component of a version string (``'2.7' -> '2'``)."""
    return version.split(".", 1)[0]


def check_version(
    version: str,
    supported_majors: Sequence[str],
    *,
    label: str = "wire schema",
    error: type[Exception] = VersionError,
) -> None:
    """Raise if *version*'s major is not supported.

    The single gate every deserialization path runs through, so an incompatible
    payload is refused with a clear, consistent message across the suite. Pass
    ``error`` to raise a package's own exception type (e.g. ``ProofError``) so
    version failures fold into that package's error hierarchy.
    """
    if schema_major(version) not in tuple(supported_majors):
        understood = ", ".join(f"{m}.x" for m in supported_majors)
        raise error(f"{label} {version} is not supported (this build understands {understood}) — upgrade or regenerate")


def make_versioned_base(
    field_name: str,
    current: str,
    supported_majors: Sequence[str],
    *,
    label: str | None = None,
    class_name: str = "VersionedBase",
    error: type[Exception] = VersionError,
) -> type[BaseModel]:
    """Build a Pydantic base that stamps and version-checks *field_name*.

    Subclasses of the returned base carry ``field_name`` (defaulting to
    *current*) and, on construction or ``model_validate``, refuse a payload
    whose major is not in *supported_majors*. Keeping the field name a parameter
    lets each package keep its readable name (``proof_version``, …) while sharing
    the logic; ``error`` selects the exception type raised on a mismatch.
    """
    majors = tuple(supported_majors)
    lbl = label or field_name

    def _check_wire_version(self: BaseModel) -> BaseModel:
        check_version(getattr(self, field_name), majors, label=lbl, error=error)
        return self

    namespace = {
        "__annotations__": {field_name: str},
        field_name: current,
        "_check_wire_version": model_validator(mode="after")(_check_wire_version),
    }
    return type(class_name, (BaseModel,), namespace)


def wire_schema(models: Iterable[type[BaseModel]], *, title: str) -> dict:
    """Combined JSON Schema for *models* (serialization view, ``$defs`` refs)."""
    from pydantic.json_schema import models_json_schema

    _, schema = models_json_schema(
        [(m, "serialization") for m in models],
        title=title,
        ref_template="#/$defs/{model}",
    )
    return schema


def render_schema(models: Iterable[type[BaseModel]], *, title: str) -> str:
    """The canonical committed-schema string: sorted, indented, newline-terminated.

    Used by both ``schema`` (write) and ``schema --check`` (compare) so the two
    can never disagree about formatting.
    """
    return json.dumps(wire_schema(models, title=title), indent=2, sort_keys=True) + "\n"
