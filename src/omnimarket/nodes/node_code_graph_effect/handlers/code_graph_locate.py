# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where each node lives and which non-Python files belong to it, from the overlay's roots (OMN-26202).

A node is a directory with a ``contract.yaml`` the overlay's ``contract_globs`` match. Its files are
the directory, that contract, the ``metadata.yaml`` / ``overlay.yaml`` / ``overlay.example.yaml``
beside it, and the workflow or unit command files the overlay's ``command_globs`` match that name the
node's package, a module of it or a path inside its directory. The snapshot is the whole input: no
walk beyond the files the existing roots already read.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.models.model_code_graph_lookup import (
    EnumNodeFileKind,
    ModelNodeFile,
)
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    CONTRACT_FILE,
    NODE_COMPANION_FILES,
    GraphRules,
    command_modules,
    command_paths,
    glob_regex,
)

_KIND_ORDER = {kind: index for index, kind in enumerate(EnumNodeFileKind)}


def _is_contract(path: str, rules: GraphRules) -> bool:
    return path.rsplit("/", 1)[-1] == CONTRACT_FILE and any(
        glob_regex(glob).match(path) for glob in rules.roots.contract_globs
    )


def _names_node(text: str, directory: str, package: str) -> bool:
    prefix = f"{directory}/"
    return (
        prefix in text
        or any(
            module == package or module.startswith(f"{package}.")
            for module in command_modules(text)
        )
        or any(path.startswith(prefix) for path in command_paths(text))
    )


def node_files_of(
    files: Mapping[str, str], rules: GraphRules
) -> tuple[ModelNodeFile, ...]:
    """Every node's files in the snapshot, sorted by node, kind and path."""
    commands = {
        path: text
        for path, text in files.items()
        if any(glob_regex(glob).match(path) for glob in rules.roots.command_globs)
    }
    found: list[ModelNodeFile] = []
    for path in sorted(files):
        if not _is_contract(path, rules):
            continue
        directory = path.rsplit("/", 1)[0] if "/" in path else "."
        node = directory.rsplit("/", 1)[-1]
        found.append(
            ModelNodeFile(node=node, kind=EnumNodeFileKind.DIRECTORY, path=directory)
        )
        found.append(
            ModelNodeFile(node=node, kind=EnumNodeFileKind.CONTRACT, path=path)
        )
        for name in NODE_COMPANION_FILES:
            sibling = f"{directory}/{name}"
            if sibling in files:
                kind = (
                    EnumNodeFileKind.METADATA
                    if name == "metadata.yaml"
                    else EnumNodeFileKind.OVERLAY
                )
                found.append(ModelNodeFile(node=node, kind=kind, path=sibling))
        package = rules.module_name(f"{directory}/__init__.py")
        for command, text in sorted(commands.items()):
            if _names_node(text, directory, package):
                found.append(
                    ModelNodeFile(
                        node=node, kind=EnumNodeFileKind.WORKFLOW, path=command
                    )
                )
    return tuple(sorted(found, key=lambda f: (f.node, _KIND_ORDER[f.kind], f.path)))
