# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AST-based import scanner — fallback when graphify is unavailable.

Walks all .py files under root, extracts import edges via ast.parse + ast.walk,
and identifies orphan modules (no inbound edges, not entry-point __main__).
"""

from __future__ import annotations

import ast
from pathlib import Path

from omnimarket.nodes.node_dependency_health_sweep.models.model_graph_types import (
    ModelImportGraph,
)


def _module_name(path: Path, root: Path) -> str:
    """Convert a file path relative to root into a dotted module name."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    parts = list(rel.parts)
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _extract_imports(source: str) -> list[str]:
    """Return top-level module names referenced in import statements."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module.split(".")[0])
    return names


class ASTImportScanner:
    """Scan a source tree for import edges using the ast module."""

    def scan(self, root: Path) -> ModelImportGraph:
        py_files = sorted(root.rglob("*.py"))
        # Map stem name → relative path string for each discovered file
        module_paths: dict[str, str] = {}
        for f in py_files:
            mod = _module_name(f, root)
            module_paths[mod] = str(f.relative_to(root))

        # Build edge list: (importer_mod, importee_mod) where importee is local
        edges: list[tuple[str, str]] = []
        # Track which modules have at least one inbound edge
        has_inbound: set[str] = set()

        for f in py_files:
            importer = _module_name(f, root)
            try:
                source = f.read_text(encoding="utf-8")
            except OSError:
                continue
            for imported_top in _extract_imports(source):
                # Only record edges to modules that exist in this tree
                for mod in module_paths:
                    if mod == imported_top or mod.startswith(imported_top + "."):
                        edges.append((importer, mod))
                        has_inbound.add(mod)

        all_mods = list(module_paths.keys())
        # Orphan: no inbound edges and not __main__
        orphans = [
            module_paths[m]
            for m in all_mods
            if m not in has_inbound and not m.endswith("__main__")
        ]

        return ModelImportGraph(
            nodes=[module_paths[m] for m in all_mods],
            edges=edges,
            orphan_modules=orphans,
        )
