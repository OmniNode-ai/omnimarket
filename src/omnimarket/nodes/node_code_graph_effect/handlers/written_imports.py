# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each import as the file wrote it, with the uses that give it meaning (OMN-26197).

The graph resolves an import to a file and drops one it cannot resolve, so a name another repository
removed leaves no trace. This pass keeps every import of one parsed file as written (module, name,
alias, line, qualified by the file's package when relative), the attribute uses of the names an import
bound (``m.f`` after ``import a.mod as m``), and, for a file with a star import, the bare names it
reads and binds nowhere itself. A name is looked up through the scopes the way Python does: the
nearest enclosing function or module scope that binds it, the class body only for itself, the last
binding at or before the use inside the use's own scope. A use through a name some other statement
rebound is not a use of the import. It reads no file, clock, network or environment.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass, field

from omnimarket.models.model_code_graph import (
    EnumWrittenImportKind,
    ModelAttributeUse,
    ModelFileReferences,
    ModelNameUse,
    ModelWrittenImport,
)

Position = tuple[int, int]

_IMPORT_MODULE = "importlib.import_module"
_DUNDER_IMPORT = "builtins.__import__"
_LITERAL_CALLS = frozenset({_IMPORT_MODULE, _DUNDER_IMPORT})
_VIA = {"alias": "module", "from": "from-name", "import-module": "import-module"}


def is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def from_base(node: ast.ImportFrom, package: Sequence[str]) -> str:
    """The dotted module a ``from`` import names, qualified by the file's package when relative."""
    if not node.level:
        return node.module or ""
    keep = len(package) - (node.level - 1)
    if keep < 0:
        return ""
    return ".".join([*package[:keep], *([node.module] if node.module else [])])


@dataclass
class _Binding:
    name: str
    pos: Position
    #: ``package`` (``import a.b`` binds ``a``), ``alias`` (``import a.b as m``), ``from``
    #: (``from a import b``), ``import-module`` (``m = import_module("a.b")``), ``pending`` (a call
    #: assigned to a name, not yet judged) or ``other`` (any other way a name is bound).
    kind: str
    #: The dotted name the binding stands for (the module, or ``base.name`` for ``from``).
    target: str = ""
    #: ``package`` only: the full dotted module the statement imported.
    written: str = ""
    call: ast.Call | None = None


@dataclass
class _Scope:
    kind: str
    parent: _Scope | None
    bindings: dict[str, list[_Binding]] = field(default_factory=dict)

    def bind(self, binding: _Binding) -> None:
        self.bindings.setdefault(binding.name, []).append(binding)


@dataclass
class _Chain:
    scope: _Scope
    root: ast.expr
    parts: tuple[str, ...]
    line: int


@dataclass
class _Call:
    scope: _Scope
    node: ast.Call
    typing_only: bool


def _literal_arg(node: ast.Call) -> str | None:
    if (
        node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


class _Collector(ast.NodeVisitor):
    def __init__(self, package: Sequence[str]) -> None:
        self.package = package
        self.module = _Scope("module", None)
        self.scopes = [self.module]
        self.scope = self.module
        self.typing_only = False
        self.imports: list[ModelWrittenImport] = []
        self.chains: list[_Chain] = []
        self.calls: list[_Call] = []
        self.loads: list[tuple[_Scope, str, Position]] = []
        self.all_names: list[str] = []

    def _pos(
        self,
        node: ast.expr | ast.stmt | ast.arg | ast.pattern | ast.excepthandler,
    ) -> Position:
        return (node.lineno, node.col_offset)

    def _bind_other(self, name: str, pos: Position) -> None:
        self.scope.bind(_Binding(name, pos, "other"))

    def visit_If(self, node: ast.If) -> None:
        if is_type_checking(node.test):
            outer = self.typing_only
            self.typing_only = True
            for child in node.body:
                self.visit(child)
            self.typing_only = outer
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            pos = self._pos(node)
            if alias.asname:
                self.scope.bind(_Binding(alias.asname, pos, "alias", target=alias.name))
            else:
                head = alias.name.split(".")[0]
                self.scope.bind(
                    _Binding(head, pos, "package", target=head, written=alias.name)
                )
            self.imports.append(
                ModelWrittenImport(
                    module=alias.name,
                    alias=alias.asname,
                    line=node.lineno,
                    kind=EnumWrittenImportKind.IMPORT,
                    typing_only=self.typing_only,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = from_base(node, self.package)
        if not base:
            return
        for alias in node.names:
            star = alias.name == "*"
            if not star:
                self.scope.bind(
                    _Binding(
                        alias.asname or alias.name,
                        self._pos(node),
                        "from",
                        target=f"{base}.{alias.name}",
                    )
                )
            self.imports.append(
                ModelWrittenImport(
                    module=base,
                    name=alias.name,
                    alias=alias.asname,
                    line=node.lineno,
                    kind=EnumWrittenImportKind.STAR
                    if star
                    else EnumWrittenImportKind.FROM,
                    typing_only=self.typing_only,
                )
            )

    def _function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
    ) -> None:
        self.visit(node.args)
        if not isinstance(node, ast.Lambda):
            for decorator in node.decorator_list:
                self.visit(decorator)
            if node.returns is not None:
                self.visit(node.returns)
            self._bind_other(node.name, self._pos(node))
        outer = self.scope
        self.scope = _Scope("function", outer)
        self.scopes.append(self.scope)
        arguments = node.args
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            *([arguments.vararg] if arguments.vararg else []),
            *([arguments.kwarg] if arguments.kwarg else []),
        ):
            self._bind_other(argument.arg, self._pos(argument))
        if isinstance(node, ast.Lambda):
            self.visit(node.body)
        else:
            for child in node.body:
                self.visit(child)
        self.scope = outer

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in (*node.decorator_list, *node.bases):
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._bind_other(node.name, self._pos(node))
        outer = self.scope
        self.scope = _Scope("class", outer)
        self.scopes.append(self.scope)
        for child in node.body:
            self.visit(child)
        self.scope = outer

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self._bind_other(node.id, self._pos(node))
        elif isinstance(node.ctx, ast.Load):
            self.loads.append((self.scope, node.id, self._pos(node)))

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bind_other(node.name, self._pos(node))
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self._bind_other(node.name, self._pos(node))
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self._bind_other(node.name, self._pos(node))

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self._bind_other(node.rest, self._pos(node))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parts: list[str] = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name | ast.Call):
            self.chains.append(
                _Chain(self.scope, current, tuple(reversed(parts)), node.lineno)
            )
        self.visit(current)

    def visit_Call(self, node: ast.Call) -> None:
        if _literal_arg(node) is not None and isinstance(
            node.func, ast.Name | ast.Attribute
        ):
            self.calls.append(_Call(self.scope, node, self.typing_only))
        self.generic_visit(node)

    def _all(self, value: ast.expr) -> None:
        if isinstance(value, ast.List | ast.Tuple):
            self.all_names.extend(
                item.value
                for item in value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )

    def _assign(self, target: ast.expr, value: ast.expr | None) -> bool:
        """Bind ``name = import_module(...)`` as a pending binding; false for any other statement."""
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Call)
            and _literal_arg(value) is not None
        ):
            self.scope.bind(
                _Binding(target.id, self._pos(target), "pending", call=value)
            )
            return True
        return False

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        if self.scope is self.module and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            self._all(node.value)
        for target in node.targets:
            if not (len(node.targets) == 1 and self._assign(target, node.value)):
                self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.annotation)
        if not self._assign(node.target, node.value):
            self.visit(node.target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if (
            self.scope is self.module
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
        ):
            self._all(node.value)
        self.generic_visit(node)


def _lookup(scope: _Scope, name: str, pos: Position) -> list[_Binding]:
    """The bindings a read of ``name`` at ``pos`` in ``scope`` can reach (empty: bound nowhere here)."""
    current: _Scope | None = scope
    while current is not None:
        if current.kind != "class" or current is scope:
            found = sorted(current.bindings.get(name, ()), key=lambda b: b.pos)
            if found:
                if current is scope:
                    found = [b for b in found if b.pos <= pos] or found
                last = found[-1]
                if last.kind != "package":
                    return [last]
                run: list[_Binding] = []
                for binding in reversed(found):
                    if binding.kind != "package":
                        break
                    run.append(binding)
                return run
        current = current.parent
    return []


def _dotted(
    scope: _Scope, root: ast.Name, tail: Sequence[str], pos: Position
) -> tuple[str, str, str] | None:
    """``(dotted, via, anchor)`` of ``root.tail`` read through the import binding ``root`` reaches."""
    for binding in _lookup(scope, root.id, pos):
        if binding.kind == "package":
            dotted = ".".join([binding.name, *tail])
            if dotted == binding.written or dotted.startswith(binding.written + "."):
                return dotted, "module", binding.written
        elif binding.kind in _VIA:
            return ".".join([binding.target, *tail]), _VIA[binding.kind], binding.target
    return None


def _is_literal_import(scope: _Scope, call: ast.Call) -> bool:
    """The call is ``importlib.import_module`` (or the builtin ``__import__``) with a literal argument."""
    pos = (call.lineno, call.col_offset)
    tail: list[str] = []
    root = call.func
    while isinstance(root, ast.Attribute):
        tail.append(root.attr)
        root = root.value
    if not isinstance(root, ast.Name):
        return False
    resolved = _dotted(scope, root, list(reversed(tail)), pos)
    if resolved is not None:
        return resolved[0] in _LITERAL_CALLS
    return root.id == "__import__" and not tail and not _lookup(scope, root.id, pos)


def scan_written(
    tree: ast.AST, package: Sequence[str], path: str
) -> ModelFileReferences:
    """Everything ``path`` writes about other modules; empty when it imports nothing."""
    collector = _Collector(package)
    collector.visit(tree)

    literal: dict[int, str] = {}
    imports = list(collector.imports)
    for call in collector.calls:
        value = _literal_arg(call.node)
        if value is not None and _is_literal_import(call.scope, call.node):
            literal[id(call.node)] = value
            imports.append(
                ModelWrittenImport(
                    module=value,
                    line=call.node.lineno,
                    kind=EnumWrittenImportKind.IMPORT_MODULE,
                    typing_only=call.typing_only,
                )
            )

    for scope in collector.scopes:
        for bindings in scope.bindings.values():
            for binding in bindings:
                if binding.kind == "pending" and binding.call is not None:
                    module = literal.get(id(binding.call))
                    binding.kind = "other" if module is None else "import-module"
                    binding.target = module or ""

    uses: dict[tuple[str, int, str, str], ModelAttributeUse] = {}
    for chain in collector.chains:
        root = chain.root
        found: tuple[str, str, str] | None
        if isinstance(root, ast.Name):
            found = _dotted(
                chain.scope, root, chain.parts, (root.lineno, root.col_offset)
            )
        elif isinstance(root, ast.Call) and id(root) in literal:
            module = literal[id(root)]
            found = (".".join([module, *chain.parts]), "import-module", module)
        else:
            found = None
        if found is not None:
            dotted, via, anchor = found
            uses[(dotted, chain.line, via, anchor)] = ModelAttributeUse(
                dotted=dotted,
                line=chain.line,
                via=via,
                anchor=anchor,
            )

    free: dict[str, int] = {}
    if any(item.kind is EnumWrittenImportKind.STAR for item in imports):
        for scope, name, pos in collector.loads:
            if not _lookup(scope, name, pos):
                free.setdefault(name, pos[0])

    return ModelFileReferences(
        path=path,
        imports=tuple(imports),
        attribute_uses=tuple(sorted(uses.values(), key=lambda u: (u.line, u.dotted))),
        free_uses=tuple(
            ModelNameUse(name=name, line=line) for name, line in sorted(free.items())
        ),
        all_names=tuple(collector.all_names),
    )
