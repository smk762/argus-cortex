"""Anchored ``match``/``search`` id checks become ``fullmatch`` (see the package docstring)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import libcst as cst

try:  # Python 3.11+
    import re._constants as _c
    import re._parser as _p
except ImportError:  # pragma: no cover
    import sre_constants as _c  # type: ignore[no-redef]
    import sre_parse as _p  # type: ignore[no-redef]

# The default safe_name pattern, with and without anchors: a check like this guards a path segment.
_SAFE_NAME = re.compile(r"\^?\[A-Za-z0-9\]\[A-Za-z0-9\._-\]\*\$?|\^?\[A-Za-z0-9\]\[A-Za-z0-9\._-\]\{0,\d+\}\$?")


def anchored(pattern: str, flags: int = 0) -> bool:
    """True when ``^...$`` spans the whole pattern, so ``fullmatch`` means the same minus the newline."""
    if flags & re.MULTILINE:
        return False
    try:
        parsed = _p.parse(pattern, flags)
    except re.error:
        return False
    if parsed.state.flags & re.MULTILINE:  # an inline (?m)
        return False
    data = list(parsed.data)
    return (
        len(data) >= 2
        and data[0] == (_c.AT, _c.AT_BEGINNING)
        and data[-1] == (_c.AT, _c.AT_END)
        and not any(op is _c.BRANCH for op, _ in data)
    )


def _string(node: cst.BaseExpression) -> str | None:
    if isinstance(node, cst.SimpleString | cst.ConcatenatedString):
        value = node.evaluated_value
        return value if isinstance(value, str) else None
    return None


def _flags(node: cst.BaseExpression | None, re_names: set[str]) -> int | None:
    """The value of a flags expression built from ``re.X`` names and ``|``; None when unknown."""
    if node is None:
        return 0
    if isinstance(node, cst.BinaryOperation) and isinstance(node.operator, cst.BitOr):
        left, right = _flags(node.left, re_names), _flags(node.right, re_names)
        return None if left is None or right is None else left | right
    if isinstance(node, cst.Attribute) and isinstance(node.value, cst.Name) and node.value.value in re_names:
        value = getattr(re, node.attr.value, None)
        return int(value) if isinstance(value, re.RegexFlag) else None
    if isinstance(node, cst.Integer):
        return int(node.value, 0)
    return None


def _arg(call: cst.Call, pos: int, name: str) -> cst.BaseExpression | None:
    for i, a in enumerate(call.args):
        if (a.keyword is None and i == pos) or (a.keyword is not None and a.keyword.value == name):
            return a.value
    return None


@dataclass
class Report:
    rewritten: list[str] = field(default_factory=list)  # "line N:C <pattern>" for every rewrite
    review: list[str] = field(default_factory=list)  # rewrites whose direction a person must check
    safe_name: list[str] = field(default_factory=list)  # rewritten checks that look like a path segment


class _Patterns(cst.CSTVisitor):
    """Module-level ``NAME = r"^...$"`` and ``NAME = re.compile(r"^...$", flags)``."""

    def __init__(self, re_names: set[str]) -> None:
        self.re_names = re_names
        self.strings: dict[str, str] = {}
        self.compiled: dict[str, str] = {}
        self.depth = 0

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self.depth += 1

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        self.depth -= 1

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self.depth += 1

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        self.depth -= 1

    def _assign(self, target: cst.BaseExpression, value: cst.BaseExpression | None) -> None:
        if self.depth or value is None or not isinstance(target, cst.Name):
            return
        name = target.value
        if (s := _string(value)) is not None:
            self.strings[name] = s
        elif (
            isinstance(value, cst.Call)
            and isinstance(value.func, cst.Attribute)
            and isinstance(value.func.value, cst.Name)
            and value.func.value.value in self.re_names
            and value.func.attr.value == "compile"
        ):
            pattern = _arg(value, 0, "pattern")
            p = _string(pattern) if pattern is not None else None
            if p is None and isinstance(pattern, cst.Name):
                p = self.strings.get(pattern.value)
            flags = _flags(_arg(value, 1, "flags"), self.re_names)
            if p is not None and flags is not None and anchored(p, flags):
                self.compiled[name] = p

    def visit_Assign(self, node: cst.Assign) -> None:
        if len(node.targets) == 1:
            self._assign(node.targets[0].target, node.value)

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        self._assign(node.target, node.value)


class _Rewrite(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (cst.metadata.PositionProvider, cst.metadata.ParentNodeProvider)

    def __init__(self, patterns: _Patterns, report: Report) -> None:
        self.p, self.report = patterns, report

    def _refuses_on_no_match(self, node: cst.Call) -> bool:
        """``not X.match(v)`` or ``X.match(v) is None``: the check refuses what doesn't match, so
        ``fullmatch`` only tightens it. Any other use (refusing on a match, parsing, returning the
        result) may loosen or change behaviour, and is listed for review."""
        parent = self.get_metadata(cst.metadata.ParentNodeProvider, node, None)
        if isinstance(parent, cst.UnaryOperation) and isinstance(parent.operator, cst.Not):
            return True
        if isinstance(parent, cst.Comparison) and parent.left is node and len(parent.comparisons) == 1:
            target = parent.comparisons[0]
            return (
                isinstance(target.operator, cst.Is)
                and isinstance(target.comparator, cst.Name)
                and target.comparator.value == "None"
            )
        return False

    def _record(self, node: cst.Call, pattern: str) -> None:
        pos = self.get_metadata(cst.metadata.PositionProvider, node).start
        where = f"line {pos.line}:{pos.column + 1}: {pattern}"
        self.report.rewritten.append(where)
        if not self._refuses_on_no_match(node):
            self.report.review.append(where)
        if _SAFE_NAME.fullmatch(pattern):
            self.report.safe_name.append(where)

    def leave_Call(self, original: cst.Call, updated: cst.Call) -> cst.BaseExpression:
        func = updated.func
        if not isinstance(func, cst.Attribute) or func.attr.value not in {"match", "search"}:
            return updated
        owner = func.value
        pattern: str | None = None
        if isinstance(owner, cst.Name) and owner.value in self.p.re_names:  # re.match(P, s, flags)
            arg = _arg(updated, 0, "pattern")
            pattern = _string(arg) if arg is not None else None
            if pattern is None and isinstance(arg, cst.Name):
                pattern = self.p.strings.get(arg.value)
            flags = _flags(_arg(updated, 2, "flags"), self.p.re_names)
            if pattern is None or flags is None or not anchored(pattern, flags):
                return updated
        elif isinstance(owner, cst.Name) and owner.value in self.p.compiled:  # PATTERN.match(s)
            if len(updated.args) != 1:  # pos/endpos change the meaning
                return updated
            pattern = self.p.compiled[owner.value]
        else:
            return updated
        self._record(original, pattern)
        return updated.with_changes(func=func.with_changes(attr=cst.Name("fullmatch")))


class _ReImports(cst.CSTVisitor):
    """Every name ``re`` is imported as, at any depth (a function-local ``import re as _re`` too)."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Import(self, node: cst.Import) -> None:
        for alias in node.names:
            if isinstance(alias.name, cst.Name) and alias.name.value == "re":
                self.names.add(alias.asname.name.value if alias.asname else "re")  # type: ignore[union-attr]


def _re_names(module: cst.Module) -> set[str]:
    found = _ReImports()
    module.visit(found)
    return found.names


def transform(source: str) -> tuple[str, Report]:
    """Rewrite *source*; return the new source and what changed."""
    module = cst.parse_module(source)
    report = Report()
    re_names = _re_names(module)
    if not re_names:
        return source, report
    patterns = _Patterns(re_names)
    module.visit(patterns)
    wrapper = cst.metadata.MetadataWrapper(module)
    new = wrapper.visit(_Rewrite(patterns, report))
    return new.code, report


def summary(report: Report) -> list[str]:
    review = set(report.review)
    out = [f"{'REVIEW' if r in review else 'tightens'} fullmatch: {r}" for r in report.rewritten]
    out += [f"consider argus_cortex.fs.safe_name: {r}" for r in report.safe_name]
    return out


LEGEND = (
    "tightens: the check refuses a non-match (`not X.match`, `is None`), so a trailing newline is now refused too. "
    "REVIEW: anything else. A check that refuses on a match is loosened by fullmatch, and a parser of lines that "
    "keep their newline stops matching; revert those to .match. Run the formatter afterwards."
)
