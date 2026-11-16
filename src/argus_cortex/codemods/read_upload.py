"""``await upload.read()`` on an ``UploadFile`` becomes ``read_upload`` (see the package docstring)."""

from __future__ import annotations

from dataclasses import dataclass, field

import libcst as cst
from libcst.codemod import CodemodContext
from libcst.codemod.visitors import AddImportsVisitor

CAP = "MAX_UPLOAD_BYTES"
CAP_LINE = f"{CAP} = 50 * 1024 * 1024  # per upload; argus_cortex.limits.read_upload raises TooLarge over it\n"


@dataclass
class Report:
    rewritten: list[str] = field(default_factory=list)
    added_cap: bool = False


def _is_upload(annotation: cst.BaseExpression) -> bool:
    if isinstance(annotation, cst.Name):
        return annotation.value == "UploadFile"
    if isinstance(annotation, cst.Attribute):
        return annotation.attr.value == "UploadFile"
    if isinstance(annotation, cst.BinaryOperation):  # UploadFile | None
        return _is_upload(annotation.left) or _is_upload(annotation.right)
    if isinstance(annotation, cst.Subscript) and isinstance(annotation.value, cst.Name | cst.Attribute):
        outer = annotation.value.value if isinstance(annotation.value, cst.Name) else annotation.value.attr.value
        if outer == "Optional":
            return any(isinstance(e.slice, cst.Index) and _is_upload(e.slice.value) for e in annotation.slice)
    return False


def _is_upload_list(annotation: cst.BaseExpression) -> bool:
    if isinstance(annotation, cst.Subscript) and isinstance(annotation.value, cst.Name | cst.Attribute):
        outer = annotation.value.value if isinstance(annotation.value, cst.Name) else annotation.value.attr.value
        if outer in {"list", "List", "Sequence"}:
            return any(isinstance(e.slice, cst.Index) and _is_upload(e.slice.value) for e in annotation.slice)
    return False


class _Rewrite(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (cst.metadata.PositionProvider,)

    def __init__(self, report: Report) -> None:
        self.report = report
        self.scopes: list[tuple[set[str], set[str]]] = []  # (uploads, lists of uploads) per function

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        uploads: set[str] = set()
        lists: set[str] = set()
        params = [*node.params.posonly_params, *node.params.params, *node.params.kwonly_params]
        for p in params:
            if p.annotation is None:
                continue
            if _is_upload(p.annotation.annotation):
                uploads.add(p.name.value)
            elif _is_upload_list(p.annotation.annotation):
                lists.add(p.name.value)
        self.scopes.append((uploads, lists))

    def leave_FunctionDef(self, original: cst.FunctionDef, updated: cst.FunctionDef) -> cst.FunctionDef:
        self.scopes.pop()
        return updated

    def visit_For(self, node: cst.For) -> None:
        # for f in files:  /  for i, f in enumerate(files):
        if not self.scopes:
            return
        uploads, lists = self.scopes[-1]
        it, target = node.iter, node.target
        if isinstance(it, cst.Name) and it.value in lists and isinstance(target, cst.Name):
            uploads.add(target.value)
        elif (
            isinstance(it, cst.Call)
            and isinstance(it.func, cst.Name)
            and it.func.value == "enumerate"
            and it.args
            and isinstance(it.args[0].value, cst.Name)
            and it.args[0].value.value in lists
            and isinstance(target, cst.Tuple)
            and len(target.elements) == 2
            and isinstance(target.elements[1].value, cst.Name)
        ):
            uploads.add(target.elements[1].value.value)

    def leave_Await(self, original: cst.Await, updated: cst.Await) -> cst.BaseExpression:
        call = updated.expression
        if not (
            self.scopes
            and isinstance(call, cst.Call)
            and not call.args
            and isinstance(call.func, cst.Attribute)
            and call.func.attr.value == "read"
            and isinstance(call.func.value, cst.Name)
            and call.func.value.value in self.scopes[-1][0]
        ):
            return updated
        name = call.func.value.value
        line = self.get_metadata(cst.metadata.PositionProvider, original).start.line
        self.report.rewritten.append(f"line {line}: await {name}.read()")
        new_call = cst.Call(func=cst.Name("read_upload"), args=[cst.Arg(cst.Name(name)), cst.Arg(cst.Name(CAP))])
        return updated.with_changes(expression=new_call)


def _defines(module: cst.Module, name: str) -> bool:
    for stmt in module.body:
        for small in stmt.body if isinstance(stmt, cst.SimpleStatementLine) else ():
            if isinstance(small, cst.Assign | cst.AnnAssign):
                targets = [t.target for t in small.targets] if isinstance(small, cst.Assign) else [small.target]
                if any(isinstance(t, cst.Name) and t.value == name for t in targets):
                    return True
    return False


def _with_cap(module: cst.Module) -> cst.Module:
    """Insert the cap constant after the last top-level import."""
    body = list(module.body)
    last = -1
    for i, stmt in enumerate(body):
        if isinstance(stmt, cst.SimpleStatementLine) and any(
            isinstance(s, cst.Import | cst.ImportFrom) for s in stmt.body
        ):
            last = i
    line = cst.parse_statement(CAP_LINE)
    line = line.with_changes(leading_lines=[cst.EmptyLine()])
    body.insert(last + 1, line)
    return module.with_changes(body=body)


def transform(source: str) -> tuple[str, Report]:
    module = cst.parse_module(source)
    report = Report()
    new = cst.metadata.MetadataWrapper(module).visit(_Rewrite(report))
    if not report.rewritten:
        return source, report
    context = CodemodContext()
    AddImportsVisitor.add_needed_import(context, "argus_cortex.limits", "read_upload")
    new = AddImportsVisitor(context).transform_module(new)
    if not _defines(new, CAP):
        new = _with_cap(new)
        report.added_cap = True
    return new.code, report


def summary(report: Report) -> list[str]:
    out = [f"read_upload: {r}" for r in report.rewritten]
    if report.added_cap:
        out.append(f"added {CAP} (50 MiB): set the cap this service needs, ideally from an env var")
    if report.rewritten:
        out.append(
            "map argus_cortex.limits.TooLarge to 413 and BadInput to 400 at the route or in an exception handler"
        )
    return out
