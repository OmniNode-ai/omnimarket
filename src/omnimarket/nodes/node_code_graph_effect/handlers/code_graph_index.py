# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One repository snapshot to one version of the graph: a pure function (OMN-23648).

Definitions are the module-level classes and functions of each Python file. A shape is the sorted
``name: annotation`` fields of a class with annotated fields, hashed without the class name, so two
models of different names and the same fields share a signature. Imports are the graph's own edges
inside the repository, test files included (a change reaches the tests that import it). Exemplars
are the nodes whose contract names an archetype. It reads no clock, file, network or environment.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import datetime

import yaml

from omnimarket.models.model_code_graph import ModelRepoSnapshot
from omnimarket.models.model_code_graph_lookup import (
    ModelCodeGraphVersion,
    ModelExemplarNode,
    ModelImportRelation,
    ModelShapeEntry,
    ModelSymbolDefinition,
)
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    GraphRules,
    build_graph,
    glob_regex,
)


def shape_signature(fields: tuple[str, ...]) -> str:
    """sha256 of the sorted fields; the class name is not an input."""
    text = "\n".join(sorted(fields))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _class_fields(node: ast.ClassDef) -> tuple[str, ...]:
    return tuple(
        f"{statement.target.id}: {ast.unparse(statement.annotation)}"
        for statement in node.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
    )


def _definitions(
    path: str, tree: ast.Module
) -> tuple[list[ModelSymbolDefinition], list[ModelShapeEntry]]:
    definitions: list[ModelSymbolDefinition] = []
    shapes: list[ModelShapeEntry] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            definitions.append(
                ModelSymbolDefinition(
                    name=node.name, kind="class", path=path, line=node.lineno
                )
            )
            fields = _class_fields(node)
            if fields:
                shapes.append(
                    ModelShapeEntry(
                        name=node.name,
                        path=path,
                        line=node.lineno,
                        signature=shape_signature(fields),
                        fields=tuple(sorted(fields)),
                    )
                )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            definitions.append(
                ModelSymbolDefinition(
                    name=node.name, kind="function", path=path, line=node.lineno
                )
            )
    return definitions, shapes


def _archetype(document: object) -> str | None:
    if not isinstance(document, dict):
        return None
    descriptor = document.get("descriptor")
    if isinstance(descriptor, dict) and isinstance(
        descriptor.get("node_archetype"), str
    ):
        return str(descriptor["node_archetype"])
    node_type = document.get("node_type")
    return node_type if isinstance(node_type, str) else None


def _exemplar(path: str, text: str) -> ModelExemplarNode | None:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    archetype = _archetype(document)
    if archetype is None:
        return None
    directory = path.rsplit("/", 1)[0] if "/" in path else "."
    name = document.get("name") if isinstance(document.get("name"), str) else None
    return ModelExemplarNode(
        path=directory,
        name=name or directory.rsplit("/", 1)[-1],
        archetype=archetype,
    )


def build_version(
    snapshot: ModelRepoSnapshot,
    rules: GraphRules,
    *,
    committed_at: datetime | None = None,
) -> ModelCodeGraphVersion:
    """The version of ``snapshot.repo`` at ``snapshot.head``, every list sorted."""
    definitions: list[ModelSymbolDefinition] = []
    shapes: list[ModelShapeEntry] = []
    exemplars: list[ModelExemplarNode] = []
    for path, text in sorted(snapshot.files.items()):
        if path.endswith(".py"):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            found, found_shapes = _definitions(path, tree)
            definitions.extend(found)
            shapes.extend(found_shapes)
        elif any(glob_regex(glob).match(path) for glob in rules.roots.contract_globs):
            exemplar = _exemplar(path, text)
            if exemplar is not None:
                exemplars.append(exemplar)
    graph = build_graph([snapshot], rules)
    imports = sorted(
        (
            ModelImportRelation(importer_path=importer[1], imported_path=imported[1])
            for importer, targets in graph.edges.items()
            for imported in targets
            if imported != importer
        ),
        key=lambda relation: (relation.importer_path, relation.imported_path),
    )
    return ModelCodeGraphVersion(
        repo=snapshot.repo,
        commit=snapshot.head,
        committed_at=committed_at,
        definitions=tuple(definitions),
        shapes=tuple(shapes),
        imports=tuple(imports),
        exemplars=tuple(sorted(exemplars, key=lambda node: node.path)),
    )
