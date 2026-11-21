# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``handlers`` question: one row per operation of each contract's handler routing (OMN-26337).

A contract is imported with the same adapter the Contract Graph IR node uses
(``omnibase_core.contract_graph``), so the node identity, the input and output model of a contract
without per-operation models, and the published and subscribed topics are the IR's, not a second
parse. The IR drops ``handler_routing``, the per-operation models, the declared failure causes and
the declared examples, so those are read from the same document. Every dotted reference is then
placed against the version's own definition index: a reference it cannot place is named on the row
as UNRESOLVED with the reason, and an operation with no observable route is NO_ROUTE. No row is
dropped. The function reads no file, clock or network: the snapshot's texts are the whole input.
"""

from __future__ import annotations

from collections.abc import Mapping

import yaml
from omnibase_core.contract_graph import (
    import_node_contract,
    supports_node_contract,
)
from omnibase_core.enums.enum_contract_graph_edge_kind import (
    EnumContractGraphEdgeKind,
)

from omnimarket.models.model_code_graph_handlers import (
    EnumHandlerReference,
    EnumHandlerResolution,
    EnumHandlerRoute,
    EnumUnresolvedReason,
    ModelHandlerExample,
    ModelHandlerRow,
    ModelUnresolvedReference,
)
from omnimarket.models.model_code_graph_lookup import ModelSymbolDefinition
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    GraphRules,
    glob_regex,
)

_PLACED = tuple[str | None, EnumUnresolvedReason | None]


class DefinitionIndex:
    """The version's classes by name and path: where a ``module.Class`` reference is defined."""

    def __init__(
        self, definitions: tuple[ModelSymbolDefinition, ...], python_files: set[str]
    ) -> None:
        self._paths: dict[str, list[str]] = {}
        self._files: set[str] = set(python_files)
        for definition in definitions:
            self._files.add(definition.path)
            if definition.kind == "class":
                self._paths.setdefault(definition.name, []).append(definition.path)
        self._packages = {part for path in self._files for part in _top_packages(path)}

    def place(self, module: str, name: str) -> _PLACED:
        """The defining file of ``module.name``, or the reason the index cannot place it."""
        if not module or not name:
            return None, EnumUnresolvedReason.NOT_DECLARED
        if module.split(".", 1)[0] not in self._packages:
            return None, EnumUnresolvedReason.MODULE_OUTSIDE_REPOSITORY
        stem = module.replace(".", "/")
        modules = [
            path
            for path in self._files
            if path == f"{stem}.py"
            or path.endswith((f"/{stem}.py", f"/{stem}/__init__.py"))
            or path == f"{stem}/__init__.py"
        ]
        if not modules:
            return None, EnumUnresolvedReason.MODULE_NOT_FOUND
        defined = sorted(path for path in self._paths.get(name, []) if path in modules)
        if not defined:
            # A package ``__init__`` re-exports the classes of its submodules: the class counts as
            # placed when exactly one file under that package defines it.
            packages = [
                path.removesuffix("__init__.py")
                for path in modules
                if path.endswith("__init__.py")
            ]
            below = sorted(
                path
                for path in self._paths.get(name, [])
                if any(path.startswith(package) for package in packages)
            )
            if len(below) == 1:
                return below[0], None
            return None, EnumUnresolvedReason.CLASS_NOT_DEFINED
        return defined[0], None


def _top_packages(path: str) -> list[str]:
    """The importable package names a file path can sit under (``src/a/b.py`` is ``a``)."""
    parts = path.split("/")
    stems = parts[1:] if parts[0] in ("src", "tests") else parts
    return [stems[0].removesuffix(".py")] if stems else []


def _dotted(value: object) -> tuple[str, str]:
    """A reference as ``(module, class)``: a dotted string, or a ``name``/``module`` mapping."""
    if isinstance(value, str) and "." in value:
        module, _, name = value.rpartition(".")
        return module, name
    if isinstance(value, dict):
        module_ref, name_ref = (
            value.get("module"),
            value.get("name") or value.get("class"),
        )
        if isinstance(module_ref, str) and isinstance(name_ref, str):
            return module_ref, name_ref
    return "", ""


def _as_text(module: str, name: str) -> str:
    return f"{module}.{name}" if module and name else ""


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _examples(value: object) -> tuple[ModelHandlerExample, ...]:
    if not isinstance(value, list):
        return ()
    found: list[ModelHandlerExample] = []
    for position, item in enumerate(value):
        if not isinstance(item, dict) or not isinstance(item.get("input"), dict):
            continue
        expected = item.get("expected_output")
        found.append(
            ModelHandlerExample(
                example_id=str(item.get("id") or f"example-{position + 1}"),
                payload=dict(item["input"]),
                expected_output=dict(expected) if isinstance(expected, dict) else None,
            )
        )
    return tuple(found)


def _operation(entry: dict[str, object], handler_name: str) -> str:
    event_model = entry.get("event_model")
    candidates = (
        entry.get("operation"),
        entry.get("event_type"),
        event_model.get("name") if isinstance(event_model, dict) else None,
        handler_name,
    )
    return next((c for c in candidates if isinstance(c, str) and c), "")


def _rows_of_contract(
    path: str,
    document: dict[str, object],
    index: DefinitionIndex,
    revision: str,
) -> list[ModelHandlerRow]:
    routing = document.get("handler_routing")
    entries = routing.get("handlers") if isinstance(routing, dict) else None
    if not isinstance(entries, list):
        return []
    import_error: str | None = None
    try:
        node, edges = import_node_contract(document, path)  # type: ignore[arg-type]
    except ValueError as exc:
        # The IR import refuses a contract it cannot read (a topic entry that is not a string); the
        # operations are still rows, with no IR topics, and the refusal rides on each of them.
        import_error = str(exc)
        node, edges = None, ()
    protocol = node.protocols[0] if node and node.protocols else None
    default_input = protocol.input_model if protocol else None
    default_output = protocol.output_model if protocol else None
    node_id = node.node_id if node else str(document.get("name") or path)
    contract_publishes = tuple(
        e.topic
        for e in edges
        if e.kind is EnumContractGraphEdgeKind.PUBLISHES and e.topic
    )
    contract_subscribes = tuple(
        e.topic
        for e in edges
        if e.kind is EnumContractGraphEdgeKind.SUBSCRIBES and e.topic
    )
    dispatch = document.get("runtime_dispatch")
    command_topic = (
        dispatch.get("command_topic") if isinstance(dispatch, dict) else None
    )
    terminal = document.get("terminal_event")
    rows: list[ModelHandlerRow] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        handler = entry.get("handler")
        handler_module, handler_class = _dotted(
            {"module": handler.get("module"), "name": handler.get("name")}
            if isinstance(handler, dict)
            else None
        )
        handler_name = handler_class or (
            str(handler.get("name") or "") if isinstance(handler, dict) else ""
        )
        # A contract's own models name its only operation; with several, each entry names its own.
        sole = len(entries) == 1
        input_ref = _dotted(
            entry.get("input_model")
            or entry.get("event_model")
            or (default_input if sole else None)
        )
        output_ref = _dotted(
            entry.get("output_model") or (default_output if sole else None)
        )
        placed = {
            EnumHandlerReference.HANDLER: (
                (handler_module, handler_class),
                index.place(handler_module, handler_class),
            ),
            EnumHandlerReference.INPUT_MODEL: (input_ref, index.place(*input_ref)),
            EnumHandlerReference.OUTPUT_MODEL: (output_ref, index.place(*output_ref)),
        }
        unresolved = tuple(
            ModelUnresolvedReference(
                reference=reference,
                declared=_as_text(*ref),
                reason=reason,
            )
            for reference, (ref, (_, reason)) in placed.items()
            if reason is not None
        )
        subscribes = (
            (str(entry["topic"]),)
            if isinstance(entry.get("topic"), str)
            else contract_subscribes
        )
        has_output = (isinstance(terminal, str) and bool(terminal)) or bool(
            contract_publishes
        )
        if isinstance(command_topic, str) and command_topic:
            route = EnumHandlerRoute.COMMAND_TOPIC
        elif subscribes and has_output:
            route = EnumHandlerRoute.SUBSCRIBE_TOPIC
        else:
            route = EnumHandlerRoute.NO_ROUTE
        rows.append(
            ModelHandlerRow(
                contract=path,
                node=node_id,
                operation=_operation(entry, handler_name),
                handler_class=handler_class,
                handler_module=handler_module,
                handler_path=placed[EnumHandlerReference.HANDLER][1][0],
                input_model=_as_text(*input_ref),
                input_model_path=placed[EnumHandlerReference.INPUT_MODEL][1][0],
                output_model=_as_text(*output_ref),
                output_model_path=placed[EnumHandlerReference.OUTPUT_MODEL][1][0],
                route=route,
                command_topic=command_topic if isinstance(command_topic, str) else None,
                subscribe_topics=subscribes,
                publish_topics=contract_publishes,
                terminal_event=terminal if isinstance(terminal, str) else None,
                failure_causes=_strings(entry.get("failure_causes")),
                examples=_examples(entry.get("examples")),
                resolution=(
                    EnumHandlerResolution.UNRESOLVED
                    if unresolved
                    else EnumHandlerResolution.RESOLVED
                ),
                unresolved=unresolved,
                contract_import_error=import_error,
                revision=revision,
            )
        )
    return rows


def handler_rows_of(
    files: Mapping[str, str],
    rules: GraphRules,
    definitions: tuple[ModelSymbolDefinition, ...],
    revision: str,
) -> tuple[ModelHandlerRow, ...]:
    """Every operation of every contract the rules' contract globs name, sorted by contract then operation."""
    index = DefinitionIndex(
        definitions, {path for path in files if path.endswith(".py")}
    )
    matchers = [glob_regex(glob) for glob in rules.roots.contract_globs]
    rows: list[ModelHandlerRow] = []
    for path, text in sorted(files.items()):
        if path.endswith(".py") or not any(m.match(path) for m in matchers):
            continue
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if isinstance(document, dict) and supports_node_contract(document):
            rows.extend(_rows_of_contract(path, document, index, revision))
    return tuple(sorted(rows, key=lambda row: (row.contract, row.operation)))
