"""A committed wire schema may only break in a major release (#105).

Each committed ``*-schema.json`` is compared with the same file at the latest ``v*`` tag. A breaking
difference passes only when the release is a major one: the newest version heading in CHANGELOG.md
(the release being prepared) has a higher major than the tag. A schema that carries a ``*_version``
field (the GPU wire models) must also move that wire major, because that is what ``check_version``
refuses downstream.

Breaking: a schema file, model or field removed, a field newly required, a type narrowed, an enum value
or a ``null`` branch removed, a bound or pattern tightened, ``additionalProperties`` closed or its value
schema narrowed (a ``dict[str, X]`` whose ``X`` changed). Additive changes (a new optional field, a new
enum value, a new model, a new schema file, a looser bound) pass.

CI checks out full history with tags (argus-ci's ``fetch-depth: 0``). Without tags the test fails in
CI and skips locally.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
META = {"title", "description", "default", "examples", "readOnly", "writeOnly", "deprecated"}
UPPER = ("maxLength", "maximum", "exclusiveMaximum", "maxItems")
LOWER = ("minLength", "minimum", "exclusiveMinimum", "minItems")


# ── the comparison ───────────────────────────────────────────────────────────


def _branches(s: dict[str, Any]) -> list[dict[str, Any]]:
    return list(s["anyOf"]) if "anyOf" in s else [s]


def _types(s: dict[str, Any]) -> set[str]:
    t = s.get("type")
    return set(t) if isinstance(t, list) else {t} if t else set()


def _one(old: dict[str, Any], new: dict[str, Any], path: str) -> list[str]:
    """Breaks between two schemas with no ``anyOf`` at the top."""
    if {k: v for k, v in old.items() if k not in META} == {k: v for k, v in new.items() if k not in META}:
        return []
    if "$ref" in old or "$ref" in new:
        return (
            [] if old.get("$ref") == new.get("$ref") else [f"{path}: now refers to {new.get('$ref', 'an inline type')}"]
        )
    out: list[str] = []
    ot, nt = _types(old), _types(new)
    if nt and not ot:
        out.append(f"{path}: now restricted to type {sorted(nt)}")
    elif nt and (lost := ot - nt - ({"integer"} if "number" in nt else set())):
        out.append(f"{path}: no longer accepts type {sorted(lost)}")
    if "enum" in new or "const" in new:
        allowed = set(map(json.dumps, new.get("enum", [new.get("const")])))
        before = old.get("enum", [old["const"]] if "const" in old else None)
        if before is None:
            out.append(f"{path}: now restricted to fixed values")
        elif lost := sorted(set(map(json.dumps, before)) - allowed):
            out.append(f"{path}: values removed {lost}")
    for k in UPPER:
        if k in new and (k not in old or new[k] < old[k]):
            out.append(f"{path}: {k} tightened to {new[k]}")
    for k in LOWER:
        if k in new and (k not in old or new[k] > old[k]):
            out.append(f"{path}: {k} tightened to {new[k]}")
    for k in ("pattern", "format"):
        if k in new and new.get(k) != old.get(k):
            out.append(f"{path}: {k} changed")
    oap, nap = old.get("additionalProperties", True), new.get("additionalProperties", True)
    if oap is not False and nap is False:
        out.append(f"{path}: extra fields now refused")
    elif oap is not False and isinstance(nap, dict):  # a map's value schema (dict[str, X]), now or newly typed
        out += breaks(oap if isinstance(oap, dict) else {}, nap, f"{path}{{*}}")
    op, np_ = old.get("properties", {}), new.get("properties", {})
    out += [f"{path}.{name}: removed" for name in op if name not in np_]
    out += [
        f"{path}.{name}: now required" for name in sorted(set(new.get("required", ())) - set(old.get("required", ())))
    ]
    for name in op.keys() & np_.keys():
        out += breaks(op[name], np_[name], f"{path}.{name}")
    if "items" in old and "items" in new:
        out += breaks(old["items"], new["items"], f"{path}[]")
    return out


def breaks(old: dict[str, Any], new: dict[str, Any], path: str = "$") -> list[str]:
    """Every way a document valid under *old* may be refused by *new*, or read differently."""
    out: list[str] = []
    nb = _branches(new)
    for ob in _branches(old):
        found = [_one(ob, n, path) for n in nb]
        if not any(not f for f in found):
            label = ob.get("$ref") or ob.get("type") or "a branch"
            close = min(found, key=len)
            out += close if len(nb) == 1 else [f"{path}: no longer accepts {label}"]
    return out


def schema_breaks(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    od, nd = old.get("$defs", {}), new.get("$defs", {})
    out = [f"{name}: model removed" for name in od if name not in nd]
    for name in od.keys() & nd.keys():
        out += breaks(od[name], nd[name], name)
    top_old = {k: v for k, v in old.items() if k != "$defs"}
    top_new = {k: v for k, v in new.items() if k != "$defs"}
    return sorted(out + breaks(top_old, top_new))


def wire_majors(schema: dict[str, Any]) -> set[str]:
    """The majors of every ``*_version`` field's default in *schema* (empty when unversioned).

    Both the root model's own fields and every model in ``$defs``: pydantic puts the root's fields at the top.
    """
    return {
        str(p["default"]).split(".")[0]
        for d in [schema, *schema.get("$defs", {}).values()]
        for name, p in d.get("properties", {}).items()
        if name.endswith("_version") and "default" in p
    }


def _major(version: str) -> int:
    return int(version.lstrip("v").split(".")[0])


def release_version(changelog: str) -> str | None:
    """The newest dated version heading (``## [X.Y.Z]``); ``Unreleased`` doesn't count."""
    m = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M)
    return m.group(1) if m else None


def _not_major(tag: str, releasing: str | None) -> str | None:
    if releasing is None or _major(releasing) <= _major(tag):
        return f"the release after {tag} is not a major one (CHANGELOG: {releasing or 'no new version heading'})"
    return None


def removed(at_tag: set[str], now: set[str], tag: str, releasing: str | None) -> list[str]:
    """A schema file shipped at *tag* and gone now (deleted or renamed) is a break: allowed only in a major."""
    why = _not_major(tag, releasing)
    return [f"{name}: removed since {tag} — {why}" for name in sorted(at_tag - now)] if why else []


def verdict(old: dict[str, Any], new: dict[str, Any], tag: str, releasing: str | None) -> list[str]:
    """The reasons this schema change can't ship in *releasing* (empty when it can)."""
    found = schema_breaks(old, new)
    if not found:
        return []
    why = [w] if (w := _not_major(tag, releasing)) else []
    if (om := wire_majors(old)) and wire_majors(new) == om:
        why.append(f"the wire major is still {sorted(om)}")
    return [f"{b} — {' and '.join(why)}" for b in found] if why else []


# ── against the last release ─────────────────────────────────────────────────


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout


def _latest_tag() -> str | None:
    try:
        tags = _git("tag", "-l", "v*", "--sort=-v:refname").split()
    except (OSError, subprocess.CalledProcessError):
        return None
    return tags[0] if tags else None


def test_committed_schemas_break_only_in_a_major_release() -> None:
    tag = _latest_tag()
    if tag is None:
        if os.environ.get("CI"):
            pytest.fail("no v* tag: CI must check out full history with tags (fetch-depth: 0)")
        pytest.skip("no v* tags in this checkout")
    releasing = release_version((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    at_tag = {n for n in _git("ls-tree", "--name-only", tag).split() if n.endswith("-schema.json")}
    problems = removed(at_tag, {p.name for p in ROOT.glob("*-schema.json")}, tag, releasing)
    for path in sorted(ROOT.glob("*-schema.json")):
        try:
            old = json.loads(_git("show", f"{tag}:{path.name}"))
        except subprocess.CalledProcessError:
            continue  # new since the tag
        problems += [f"{path.name}: {p}" for p in verdict(old, json.loads(path.read_text("utf-8")), tag, releasing)]
    assert not problems, "\n".join(problems)


# ── the comparison itself ────────────────────────────────────────────────────


def obj(props: dict[str, Any], required: tuple[str, ...] = (), **kw: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required), **kw}


STR = {"type": "string"}
OPT_STR = {"anyOf": [STR, {"type": "null"}], "default": None}
KIND = {"enum": ["a", "b"], "type": "string"}


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        (obj({"a": STR, "b": STR}), obj({"a": STR}), "b: removed"),
        (obj({"a": STR}), obj({"a": STR, "b": STR}, ("b",)), "b: now required"),
        (obj({"a": OPT_STR}), obj({"a": OPT_STR}, ("a",)), "a: now required"),
        (obj({"a": OPT_STR}), obj({"a": STR}), "no longer accepts type ['null']"),
        (obj({"k": KIND}), obj({"k": {"enum": ["a"], "type": "string"}}), "values removed"),
        (obj({"k": STR}), obj({"k": KIND}), "fixed values"),
        (obj({"n": {"type": "number"}}), obj({"n": {"type": "integer"}}), "no longer accepts type ['number']"),
        (
            obj({"s": {"type": "string", "maxLength": 100}}),
            obj({"s": {"type": "string", "maxLength": 50}}),
            "maxLength",
        ),
        (obj({"n": {"type": "integer"}}), obj({"n": {"type": "integer", "minimum": 0}}), "minimum"),
        (obj({"s": STR}), obj({"s": {"type": "string", "pattern": "^x$"}}), "pattern"),
        (obj({}), obj({}, additionalProperties=False), "extra fields now refused"),
        (obj({"r": {"$ref": "#/$defs/A"}}), obj({"r": {"$ref": "#/$defs/B"}}), "now refers to"),
        (obj({"l": {"type": "array", "items": KIND}}), obj({"l": {"type": "array", "items": STR}}), ""),
        (obj({"l": {"type": "array", "items": STR}}), obj({"l": {"type": "array", "items": KIND}}), "l[]"),
        # dict[str, int] -> dict[str, str]: the map's value type changed
        (
            obj({"m": {"type": "object", "additionalProperties": {"type": "integer"}}}),
            obj({"m": {"type": "object", "additionalProperties": STR}}),
            "m{*}: no longer accepts type ['integer']",
        ),
        (
            obj({"m": {"type": "object", "additionalProperties": STR}}),
            obj({"m": {"type": "object", "additionalProperties": KIND}}),
            "m{*}: now restricted to fixed values",
        ),
        (
            obj({"m": {"type": "object"}}),
            obj({"m": {"type": "object", "additionalProperties": STR}}),
            "m{*}: now restricted to type",
        ),
    ],
)
def test_breaking_changes_are_found(old: dict, new: dict, expect: str) -> None:
    found = breaks(old, new)
    if expect:
        assert any(expect in f for f in found), found
    else:
        assert found == [], found


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (obj({"a": STR}), obj({"a": STR, "b": OPT_STR})),  # a new optional field
        (obj({"k": KIND}), obj({"k": {"enum": ["a", "b", "c"], "type": "string"}})),  # a new value
        (obj({"s": {"type": "string", "maxLength": 50}}), obj({"s": {"type": "string", "maxLength": 100}})),
        (obj({"a": STR}), obj({"a": OPT_STR})),  # now nullable
        (obj({"a": STR}, ("a",)), obj({"a": STR})),  # no longer required
        (obj({"n": {"type": "integer"}}), obj({"n": {"type": "number"}})),
        (obj({"a": {**STR, "description": "x"}}), obj({"a": {**STR, "description": "y", "title": "A"}})),
        (  # a map's value type widened
            obj({"m": {"type": "object", "additionalProperties": KIND}}),
            obj({"m": {"type": "object", "additionalProperties": STR}}),
        ),
        (obj({"m": {"type": "object", "additionalProperties": STR}}), obj({"m": {"type": "object"}})),
        (obj({}, additionalProperties=False), obj({}, additionalProperties=STR)),
    ],
)
def test_additive_changes_pass(old: dict, new: dict) -> None:
    assert breaks(old, new) == []


def test_a_removed_model_breaks_and_a_new_one_does_not() -> None:
    assert schema_breaks({"$defs": {"A": obj({}), "B": obj({})}}, {"$defs": {"A": obj({})}}) == ["B: model removed"]
    assert schema_breaks({"$defs": {"A": obj({})}}, {"$defs": {"A": obj({}), "C": obj({})}}) == []


def test_a_break_needs_a_major_release_and_a_wire_major() -> None:
    v = {"type": "string", "default": "1.0"}
    old = {"$defs": {"M": obj({"lease_version": v, "x": STR})}}
    broken = {"$defs": {"M": obj({"lease_version": v})}}
    assert verdict(old, broken, "v0.8.0", "0.9.0")[0].endswith(
        "not a major one (CHANGELOG: 0.9.0) and the wire major is still ['1']"
    )
    assert "wire major" in verdict(old, broken, "v0.8.0", "1.0.0")[0]
    rebased = {"$defs": {"M": obj({"lease_version": {**v, "default": "2.0"}})}}
    assert verdict(old, rebased, "v0.8.0", "1.0.0") == []
    unversioned = {"$defs": {"M": obj({"x": STR})}}
    assert verdict(unversioned, {"$defs": {"M": obj({})}}, "v0.8.0", "1.0.0") == []
    assert verdict(unversioned, {"$defs": {"M": obj({})}}, "v0.8.0", None)


def test_a_version_on_the_root_model_is_a_wire_major() -> None:
    """pydantic puts the root model's own fields at the top, not in ``$defs``."""
    v = {"type": "string", "default": "1.0"}
    old = obj({"schema_version": v, "x": STR})
    assert wire_majors(old) == {"1"}
    broken = obj({"schema_version": v})
    assert verdict(old, broken, "v0.8.0", "1.0.0") == ["$.x: removed — the wire major is still ['1']"]
    assert verdict(old, obj({"schema_version": {**v, "default": "2.0"}}), "v0.8.0", "1.0.0") == []


def test_a_removed_or_renamed_schema_file_breaks_outside_a_major() -> None:
    at_tag = {"a-schema.json", "b-schema.json"}
    renamed = {"a-schema.json", "c-schema.json"}
    assert removed(at_tag, renamed, "v0.8.0", "0.9.0") == [
        "b-schema.json: removed since v0.8.0 — the release after v0.8.0 is not a major one (CHANGELOG: 0.9.0)"
    ]
    assert removed(at_tag, renamed, "v0.8.0", "1.0.0") == []
    assert removed(at_tag, at_tag | {"c-schema.json"}, "v0.8.0", None) == []  # a new file is additive


def test_release_version_is_the_newest_dated_heading() -> None:
    assert release_version("# Changelog\n\n## [Unreleased]\n\n## [0.9.0] - 2026-12-01\n\n## [0.8.0]\n") == "0.9.0"
    assert release_version("## [Unreleased]\n") is None
