# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule ``typed-entry``: a routed entry of a writing projection must be able to write.

The trap (OMN-18883, trap one; origin OMN-18769 C2). A contract that declares
``db_io.db_tables`` is wired by the shared runtime, and the entry it invokes is
the ``handler_routing`` class. The runtime (omnibase_infra ``handler_wiring``,
``_typed_def_b_input_model``) reads that class's
``handle`` signature first: a ``handle`` annotated with a typed request model is
wired on the TYPED dispatch arm and handed the validated request, never the
``_db``-injected dict and never ``project_event``. A projection whose routed
entry is a pure fold therefore validates every message, returns its rows in
memory and writes nothing, while the consumer commits every offset and the
topic watermarks read healthy. That is exactly how the lab lane-health
projection ran for a night with zero rows.

Refused, on a contract that declares at least one table with ``write`` or
``read_write`` access: **a runner that overrides ``handle`` with a typed fold.**
The routed class reaches ``BaseProjectionRunner`` through its bases, so it
carries a write path, but it (or a base inside the node) re-defines ``handle``
with a typed request, and its typed result declares no ``rows_upserted`` field.
The runtime takes the typed arm and the write path is never called. A typed
``handle`` whose result reports ``rows_upserted`` is a typed facade over the
write (the hook-ledger writer) and passes. A ``handle`` inherited from outside
the node (the base runner's dict entry, a shared writer base) passes.

Out of scope: a routed typed ``handle`` on a class that does not reach the
runner base. Typed effects that write through an injected service and
reducers whose typed result is applied as a projection intent take that shape
legitimately, and their write path is not visible in the syntax tree.

Pure syntax-tree and YAML reads over explicit text, no import of the handlers,
so a synthetic node is proven the same way the real tree is.
"""

import ast
from collections.abc import Sequence
from typing import Final

import yaml
from omnibase_core.models.nodes.no_utcnow_check.model_source_file import ModelSourceFile
from omnibase_core.models.validation.model_validation_finding import (
    ModelValidationFinding,
)

from omnimarket.models.contract_projection_check import (
    ModelProjectionNodeSources,
)
from omnimarket.nodes.node_contract_projection_check_compute.handlers.findings import (
    RULE_TYPED_ENTRY,
    RULE_UNPARSEABLE,
    make_finding,
)

RUNNER_BASE: Final[str] = "BaseProjectionRunner"
_WRITE_ACCESS: Final[frozenset[str]] = frozenset({"write", "read_write"})
# A first parameter annotated with one of these is the projection arm's
# injected dict (or untyped), not a typed def-B request.
_UNTYPED_ANNOTATIONS: Final[frozenset[str]] = frozenset(
    {"dict", "Dict", "Mapping", "MutableMapping", "Any", "object", "JsonType"}
)


def _annotation_name(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Subscript):
        return _annotation_name(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split("[", 1)[0].rsplit(".", 1)[-1].strip()
    return None


def _first_param_annotation(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    params = [*fn.args.posonlyargs, *fn.args.args]
    if params and params[0].arg in {"self", "cls"}:
        params = params[1:]
    if not params:
        return None
    return _annotation_name(params[0].annotation)


def _base_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


class _NodeIndex:
    """Every class and every pydantic-style field set the node's modules define."""

    def __init__(self, modules: Sequence[ModelSourceFile]) -> None:
        self.classes: dict[str, tuple[ast.ClassDef, ModelSourceFile]] = {}
        self.unparseable: list[str] = []
        for module in modules:
            try:
                tree = ast.parse(module.source)
            except SyntaxError:
                self.unparseable.append(module.path)
                continue
            for stmt in ast.walk(tree):
                if isinstance(stmt, ast.ClassDef):
                    self.classes.setdefault(stmt.name, (stmt, module))

    def lookup(self, name: str) -> tuple[ast.ClassDef, ModelSourceFile] | None:
        return self.classes.get(name)

    def reaches_runner(self, name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name == RUNNER_BASE:
            return True
        found = self.lookup(name)
        if found is None or name in seen:
            return False
        return any(
            (base := _base_name(b)) is not None
            and self.reaches_runner(base, seen | {name})
            for b in found[0].bases
        )

    def own_handle(
        self, name: str, seen: frozenset[str] = frozenset()
    ) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, str] | None:
        """The ``handle`` the class resolves to inside the node, and its owner.

        ``None`` when the method is inherited from a class outside the node
        (``BaseProjectionRunner``'s dict entry, a shared writer base): those
        take the injected dict and are not this rule's concern.
        """
        found = self.lookup(name)
        if found is None or name in seen:
            return None
        cls = found[0]
        for stmt in cls.body:
            if (
                isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                and stmt.name == "handle"
            ):
                return stmt, name
        for b in cls.bases:
            base = _base_name(b)
            if base is None or base == RUNNER_BASE:
                continue
            resolved = self.own_handle(base, seen | {name})
            if resolved is not None:
                return resolved
        return None

    def declares_field(self, name: str, field: str) -> bool:
        found = self.lookup(name)
        if found is None:
            return False
        for stmt in found[0].body:
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id == field
            ):
                return True
        return any(
            (base := _base_name(b)) is not None and self.declares_field(base, field)
            for b in found[0].bases
        )


def _write_tables(raw: object) -> list[str]:
    if not isinstance(raw, dict):
        return []
    db_io = raw.get("db_io") or {}
    if not isinstance(db_io, dict):
        return []
    tables = db_io.get("db_tables") or []
    if not isinstance(tables, list):
        return []
    return [
        str(t.get("name"))
        for t in tables
        if isinstance(t, dict) and str(t.get("access", "")) in _WRITE_ACCESS
    ]


def _routed_class_names(raw: dict[str, object]) -> list[str]:
    routing = raw.get("handler_routing")
    handlers = routing.get("handlers") if isinstance(routing, dict) else None
    names: list[str] = []
    if isinstance(handlers, list) and handlers:
        for entry in handlers:
            handler = entry.get("handler") if isinstance(entry, dict) else None
            name = handler.get("name") if isinstance(handler, dict) else None
            if isinstance(name, str) and name and name not in names:
                names.append(name)
        return names
    top = raw.get("handler")
    name = top.get("class") if isinstance(top, dict) else None
    return [name] if isinstance(name, str) and name else []


def _render_runner(
    node: str, cls: str, owner: str, request: str, result: str | None, where: str
) -> str:
    return (
        f"{node}: routed entry {cls} reaches {RUNNER_BASE}, but {owner}.handle "
        f"({where}) takes the typed request {request} and returns "
        f"{result or 'an unannotated result'}, which declares no rows_upserted. "
        "The runtime wires a typed handle on the TYPED arm (omnibase_infra "
        "handler_wiring _typed_def_b_input_model), so the "
        "runner's write path is never called: every message is validated, "
        "folded and dropped. Route the writer whose handle takes the injected "
        "dict, and call the fold from it in process (rule 7a)."
    )


def _check_node(node: ModelProjectionNodeSources) -> list[ModelValidationFinding]:
    try:
        raw = yaml.safe_load(node.contract_text)
    except yaml.YAMLError as exc:
        return [
            make_finding(
                rule_id=RULE_UNPARSEABLE,
                message=f"{node.contract_path}: unparseable contract ({exc})",
                location=node.contract_path,
                severity="ERROR",
            )
        ]
    if not isinstance(raw, dict) or not _write_tables(raw):
        return []
    index = _NodeIndex(node.modules)
    findings = [
        make_finding(
            rule_id=RULE_UNPARSEABLE,
            message=f"{path}: unparseable handler module",
            location=path,
            severity="ERROR",
        )
        for path in index.unparseable
    ]
    for cls_name in _routed_class_names(raw):
        resolved = index.own_handle(cls_name)
        if resolved is None:
            continue
        handle, owner = resolved
        request = _first_param_annotation(handle)
        if request is None or request in _UNTYPED_ANNOTATIONS:
            continue
        found = index.lookup(owner)
        if found is None:
            continue
        module = found[1]
        where = f"{module.path}:{handle.lineno}"
        if not index.reaches_runner(cls_name):
            continue
        result = _annotation_name(handle.returns)
        if result is not None and index.declares_field(result, "rows_upserted"):
            continue
        message = _render_runner(node.node, cls_name, owner, request, result, where)
        findings.append(
            make_finding(
                rule_id=RULE_TYPED_ENTRY,
                message=message,
                location=where,
                evidence={"node": node.node, "handler": cls_name, "request": request},
            )
        )
    return findings


def check_typed_entry(
    nodes: Sequence[ModelProjectionNodeSources],
) -> list[ModelValidationFinding]:
    findings: list[ModelValidationFinding] = []
    for node in nodes:
        findings.extend(_check_node(node))
    return findings
