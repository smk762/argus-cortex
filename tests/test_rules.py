"""Checks behind CLAUDE.md's "Must never happen" table that belong to no single module."""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

import argus_cortex
from argus_cortex.consent import AgeCheck
from argus_cortex.xmp import NS

SRC = Path(argus_cortex.__file__).parent

# Every optional extra's import name (pyproject [project.optional-dependencies]).
EXTRAS = (
    "httpx",
    "psycopg",
    "psycopg_pool",
    "qdrant_client",
    "minio",
    "starlette",
    "fastapi",
    "PIL",
    "pynvml",
    "yaml",
    "libcst",
)


def _modules() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in sorted(SRC.rglob("*.py"))]


def _imported(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_top_level_import_loads_no_optional_extra() -> None:
    """``import argus_cortex`` stays pydantic-only: every extra is imported lazily where it is used."""
    code = f"import sys, argus_cortex; print(','.join(m for m in {EXTRAS!r} if m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True).stdout.strip()
    assert out == "", f"import argus_cortex pulled in: {out}"


def test_cortex_never_imports_a_stage_or_nudenet() -> None:
    """Stages import cortex, never the reverse; NudeNet is AGPL and stays behind HTTP."""
    bad = {
        f"{p.relative_to(SRC)}: {name}"
        for p, tree in _modules()
        for name in _imported(tree)
        if name.split(".")[0] == "nudenet" or (name.startswith("argus_") and name.split(".")[0] != "argus_cortex")
    }
    assert not bad, sorted(bad)


def test_argus_xmp_namespace_is_pinned() -> None:
    """The URI is a wire contract: sidecars already on disk carry it."""
    assert NS["argus"] == "https://github.com/smk762/argus-cortex/ns/xmp/1.0/"


CLOCK = {"now", "today", "utcnow", "time", "monotonic", "perf_counter", "time_ns"}


def test_consent_valid_never_reads_the_clock() -> None:
    """The caller passes ``at``; a verdict must not change with the wall clock it was computed on."""
    tree = ast.parse((SRC / "consent.py").read_text(encoding="utf-8"))
    assert "time" not in _imported(tree)
    calls = [
        f"line {n.lineno}: .{n.func.attr}()"
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in CLOCK
    ]
    assert not calls, calls


@pytest.mark.parametrize("field", ["id_number", "id_scan", "document", "id_image"])
def test_an_age_check_cannot_hold_the_id(field: str) -> None:
    """A consent record keeps who checked the ID and when, never the ID or a copy of it."""
    with pytest.raises(ValidationError, match=field):
        AgeCheck(checked_by="mod", checked_on=date(2026, 1, 1), **{field: "x"})
    assert set(AgeCheck.model_fields) == {"checked_by", "checked_on"}
