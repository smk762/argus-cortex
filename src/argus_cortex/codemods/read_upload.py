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
    review: list[str] = field(default_factory=list)  # reads left as they were, for a person to check
    added_cap: bool = False


def _subscript(annotation: cst.BaseExpression) -> tuple[str, list[cst.BaseExpression]] | None:
    """``Outer[a, b]`` (or ``typing.Outer[a, b]``) as its outer name and elements."""
    if isinstance(annotation, cst.Subscript) and isinstance(annotation.value, cst.Name | cst.Attribute):
        outer = annotation.value.value if isinstance(annotation.value, cst.Name) else annotation.value.attr.value
        return outer, [e.slice.value for e in annotation.slice if isinstance(e.slice, cst.Index)]
    return None


def _is_upload(annotation: cst.BaseExpression) -> bool:
    if isinstance(annotation, cst.Name):
        return annotation.value == "UploadFile"
    if isinstance(annotation, cst.Attribute):
        return annotation.attr.value == "UploadFile"
    if isinstance(annotation, cst.BinaryOperation):  # UploadFile | None
        return _is_upload(annotation.left) or _is_upload(annotation.right)
    if sub := _subscript(annotation):
        outer, elements = sub
        if outer in {"Optional", "Union"}:
            return any(_is_upload(e) for e in elements)
        if outer == "Annotated":  # Annotated[UploadFile, File(...)]: the type is the first element only
            return bool(elements) and _is_upload(elements[0])
    return False


def _is_upload_list(annotation: cst.BaseExpression) -> bool:
    if isinstance(annotation, cst.BinaryOperation):  # list[UploadFile] | None
        return _is_upload_list(annotation.left) or _is_upload_list(annotation.right)
    if sub := _subscript(annotation):
        outer, elements = sub
        if outer in {"list", "List", "Sequence"}:
            return any(_is_upload(e) for e in elements)
        if outer in {"Optional", "Union"}:
            return any(_is_upload_list(e) for e in elements)
        if outer == "Annotated":
            return bool(elements) and _is_upload_list(elements[0])
    return False


def _target_names(target: cst.CSTNode | None) -> set[str]:
    """The names a binding target binds (``a``, ``a, *b``, ``[a, b]``; attributes and subscripts bind none)."""
    if isinstance(target, cst.Name):
        return {target.value}
    if isinstance(target, cst.Tuple | cst.List):
        return set().union(*(_target_names(e.value) for e in target.elements))
    if isinstance(target, cst.StarredElement):
        return _target_names(target.value)
    return set()


class _Bound(cst.CSTVisitor):
    """Every name a loop's body (or a ``while`` test) binds, wherever in it: an assignment after a read still rebinds
    the name before the read runs again on the next pass. A nested function or class binds in its own scope; a
    comprehension's targets are its own, but a walrus in it binds here (PEP 572)."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        self.names.add(node.name.value)
        return False

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        self.names.add(node.name.value)
        return False

    def visit_Lambda(self, node: cst.Lambda) -> bool:
        return False

    def visit_Assign(self, node: cst.Assign) -> None:
        for t in node.targets:
            self.names |= _target_names(t.target)

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        if node.value is not None:  # a bare ``file: UploadFile`` binds nothing
            self.names |= _target_names(node.target)

    def visit_AugAssign(self, node: cst.AugAssign) -> None:
        self.names |= _target_names(node.target)

    def visit_NamedExpr(self, node: cst.NamedExpr) -> None:
        self.names |= _target_names(node.target)

    def visit_WithItem(self, node: cst.WithItem) -> None:
        if node.asname is not None:
            self.names |= _target_names(node.asname.name)

    def visit_ExceptHandler(self, node: cst.ExceptHandler) -> None:
        if node.name is not None:
            self.names |= _target_names(node.name.name)

    def visit_For(self, node: cst.For) -> None:
        self.names |= _target_names(node.target)


def _bound_in(*nodes: cst.CSTNode) -> set[str]:
    bound = _Bound()
    for node in nodes:
        node.visit(bound)
    return bound.names


class _Rewrite(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (cst.metadata.PositionProvider, cst.metadata.ParentNodeProvider)

    def __init__(self, report: Report) -> None:
        self.report = report
        self.scopes: list[tuple[set[str], set[str]]] = []  # (uploads, lists of uploads) per function
        self._comp_pushed: list[bool] = []  # whether each open comprehension pushed a scope
        self._loop_rebound: list[set[str]] = []  # per open loop: names its body rebinds (left for review in it)

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        # A nested function (a helper, a closure) still sees the enclosing function's uploads,
        # except the names its own parameters rebind.
        outer_uploads, outer_lists = self.scopes[-1] if self.scopes else (set(), set())
        uploads, lists = set(outer_uploads), set(outer_lists)
        p = node.params
        params = [*p.posonly_params, *p.params, *p.kwonly_params]
        params += [s for s in (p.star_arg, p.star_kwarg) if isinstance(s, cst.Param)]
        for param in params:
            name = param.name.value
            uploads.discard(name)
            lists.discard(name)
            if param.annotation is None:
                continue
            if _is_upload(param.annotation.annotation):
                uploads.add(name)
            elif _is_upload_list(param.annotation.annotation):
                lists.add(name)
        self.scopes.append((uploads, lists))

    def leave_FunctionDef(self, original: cst.FunctionDef, updated: cst.FunctionDef) -> cst.FunctionDef:
        self.scopes.pop()
        return updated

    def _unbind(self, target: cst.CSTNode | None) -> None:
        """A name bound to something else (assigned, ``with ... as``, a loop target) is no longer an upload."""
        if not self.scopes:
            return
        if isinstance(target, cst.Name):
            for names in self.scopes[-1]:
                names.discard(target.value)
        elif isinstance(target, cst.Tuple | cst.List):
            for element in target.elements:
                self._unbind(element.value)
        elif isinstance(target, cst.StarredElement):
            self._unbind(target.value)

    # Unbound on the way out, so a read in the value (``data = await file.read()``) still sees the upload.
    def leave_Assign(self, original: cst.Assign, updated: cst.Assign) -> cst.Assign:
        for t in original.targets:
            self._unbind(t.target)
        return updated

    def leave_AnnAssign(self, original: cst.AnnAssign, updated: cst.AnnAssign) -> cst.AnnAssign:
        self._unbind(original.target)
        if self.scopes and isinstance(original.target, cst.Name):  # file: UploadFile = ...
            uploads, lists = self.scopes[-1]
            if _is_upload(original.annotation.annotation):
                uploads.add(original.target.value)
            elif _is_upload_list(original.annotation.annotation):
                lists.add(original.target.value)
        return updated

    def leave_AugAssign(self, original: cst.AugAssign, updated: cst.AugAssign) -> cst.AugAssign:
        self._unbind(original.target)
        return updated

    def leave_NamedExpr(self, original: cst.NamedExpr, updated: cst.NamedExpr) -> cst.BaseExpression:
        self._unbind(original.target)
        # Inside comprehensions it binds in the function around them all (PEP 572), so it outlives their scopes.
        comprehensions = sum(self._comp_pushed)
        if comprehensions and isinstance(original.target, cst.Name):
            for scope in self.scopes[-1 - comprehensions : -1]:
                for names in scope:
                    names.discard(original.target.value)
        return updated

    def leave_WithItem(self, original: cst.WithItem, updated: cst.WithItem) -> cst.WithItem:
        if original.asname is not None:
            self._unbind(original.asname.name)
        return updated

    def visit_ExceptHandler(self, node: cst.ExceptHandler) -> None:
        if node.name is not None:
            self._unbind(node.name.name)

    def visit_For(self, node: cst.For) -> None:
        self._loop(node.iter, node.target)
        # The target is bound afresh before each pass, so a body rebinding it never reaches the next pass's reads.
        self._enter_loop(_bound_in(node.body) - _target_names(node.target))

    def leave_For(self, original: cst.For, updated: cst.For) -> cst.BaseStatement:
        self._loop_rebound.pop()
        return updated

    def visit_While(self, node: cst.While) -> None:
        self._enter_loop(_bound_in(node.test, node.body))

    def leave_While(self, original: cst.While, updated: cst.While) -> cst.BaseStatement:
        self._loop_rebound.pop()
        return updated

    def _enter_loop(self, rebound: set[str]) -> None:
        # The linear pass only unbinds a name for code after the rebinding; in a loop, a read before it runs again
        # after it. Such a name is no upload anywhere in the loop: its reads go to review, not to read_upload.
        self._loop_rebound.append(rebound)
        if self.scopes:
            for names in self.scopes[-1]:
                names.difference_update(rebound)

    def _comprehension(self, node: cst.BaseComp) -> None:
        # A comprehension's targets are its own: they don't outlive it, so it gets a scope of its own.
        pushed = bool(self.scopes)
        self._comp_pushed.append(pushed)
        if pushed:
            uploads, lists = self.scopes[-1]
            self.scopes.append((set(uploads), set(lists)))
        # The element is visited before its ``for`` clauses, so read the clauses here, on the way in.
        clause: cst.CompFor | None = node.for_in  # type: ignore[attr-defined]
        while clause is not None:
            self._loop(clause.iter, clause.target)
            clause = clause.inner_for_in

    def _leave_comprehension(self) -> None:
        if self._comp_pushed.pop():
            self.scopes.pop()

    def visit_ListComp(self, node: cst.ListComp) -> None:
        self._comprehension(node)

    def leave_ListComp(self, original: cst.ListComp, updated: cst.ListComp) -> cst.BaseExpression:
        self._leave_comprehension()
        return updated

    def visit_SetComp(self, node: cst.SetComp) -> None:
        self._comprehension(node)

    def leave_SetComp(self, original: cst.SetComp, updated: cst.SetComp) -> cst.BaseExpression:
        self._leave_comprehension()
        return updated

    def visit_DictComp(self, node: cst.DictComp) -> None:
        self._comprehension(node)

    def leave_DictComp(self, original: cst.DictComp, updated: cst.DictComp) -> cst.BaseExpression:
        self._leave_comprehension()
        return updated

    def visit_GeneratorExp(self, node: cst.GeneratorExp) -> None:
        self._comprehension(node)

    def leave_GeneratorExp(self, original: cst.GeneratorExp, updated: cst.GeneratorExp) -> cst.BaseExpression:
        self._leave_comprehension()
        return updated

    def _loop(self, it: cst.BaseExpression, target: cst.BaseExpression) -> None:
        # for f in files:  /  for i, f in enumerate(files):  (a statement or a comprehension clause)
        if not self.scopes:
            return
        self._unbind(target)  # for file in other_things: rebinds file, whatever it was
        uploads, lists = self.scopes[-1]
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

    @staticmethod
    def _unbounded_read(call: cst.BaseExpression) -> str | None:
        """The name in ``NAME.read()`` (no arguments), else None."""
        if (
            isinstance(call, cst.Call)
            and not call.args
            and isinstance(call.func, cst.Attribute)
            and call.func.attr.value == "read"
            and isinstance(call.func.value, cst.Name)
        ):
            return call.func.value.value
        return None

    def leave_Call(self, original: cst.Call, updated: cst.Call) -> cst.BaseExpression:
        # upload.read() handed to something else (asyncio.gather, a task): no ``await`` here to rewrite.
        name = self._unbounded_read(updated)
        parent = self.get_metadata(cst.metadata.ParentNodeProvider, original, None)
        if name and self.scopes and name in self.scopes[-1][0] and not isinstance(parent, cst.Await):
            line = self.get_metadata(cst.metadata.PositionProvider, original).start.line
            self.report.review.append(f"line {line}: {name}.read() is an upload read with no await of its own")
        return updated

    def leave_Await(self, original: cst.Await, updated: cst.Await) -> cst.BaseExpression:
        name = self._unbounded_read(updated.expression)
        if not (name and self.scopes):
            return updated
        line = self.get_metadata(cst.metadata.PositionProvider, original).start.line
        if name not in self.scopes[-1][0]:
            why = (
                f"{name} is rebound in the loop around it"
                if any(name in rebound for rebound in self._loop_rebound)
                else f"{name} is not a known UploadFile"
            )
            self.report.review.append(f"line {line}: await {name}.read() ({why})")
            return updated
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


def _is_docstring(stmt: cst.CSTNode) -> bool:
    return (
        isinstance(stmt, cst.SimpleStatementLine)
        and len(stmt.body) == 1
        and isinstance(stmt.body[0], cst.Expr)
        and isinstance(stmt.body[0].value, cst.SimpleString | cst.ConcatenatedString)
    )


def _with_cap(module: cst.Module) -> cst.Module:
    """Insert the cap constant after the last top-level import, or after the docstring when there is none."""
    body = list(module.body)
    last = -1
    for i, stmt in enumerate(body):
        if isinstance(stmt, cst.SimpleStatementLine) and any(
            isinstance(s, cst.Import | cst.ImportFrom) for s in stmt.body
        ):
            last = i
    if last == -1 and body and _is_docstring(body[0]):  # above it, the string would stop being __doc__
        last = 0
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
    out += [f"REVIEW, left as it was: {r}" for r in report.review]
    if report.added_cap:
        out.append(f"added {CAP} (50 MiB): set the cap this service needs, ideally from an env var")
    if report.rewritten:
        out.append(
            "map argus_cortex.limits.TooLarge to 413 and BadInput to 400 at the route or in an exception handler"
        )
    return out
