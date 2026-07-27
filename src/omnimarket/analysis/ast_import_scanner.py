# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AST-based import scanner — exact filesystem-path resolution.

Promoted out of node_dependency_health_sweep/engine (OMN-14295) into a shared
location so node_architecture_graph_populate_effect can reuse the same
exact-resolution algorithm for its IMPORTS edges instead of duplicating a
coarser ast.walk that produced edges MERGEing against never-created nodes.
Originally built under OMN-11046 to fix an O(files x imports x modules)
edge-count explosion from prefix matching.

Walks all .py files under root, extracts import edges via ast.parse + ast.walk,
and identifies orphan modules (no inbound edges, not entry-point __main__).
"""

from __future__ import annotations

import ast
from pathlib import Path

from omnimarket.models.model_import_graph import ModelImportGraph


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
    """Return full dotted module names referenced in import statements.

    For ``import foo.bar``, returns ``"foo.bar"``.
    For ``from foo.bar import baz``, returns ``"foo.bar"``.
    Unlike the previous implementation, the top-level-only truncation is removed
    so callers can perform exact path resolution instead of prefix matching.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _resolve_import(dotted_name: str, root: Path) -> str | None:
    """Return the dotted module name for *dotted_name* if it resolves to a local file.

    Checks (in order):
    1. ``<root>/<parts...>.py``        — plain module
    2. ``<root>/<parts...>/__init__.py`` — package

    Returns ``None`` when neither candidate exists under *root*.
    """
    parts = dotted_name.split(".")
    # Candidate 1: foo/bar.py → dotted mod "foo.bar"
    candidate_file = root.joinpath(*parts).with_suffix(".py")
    if candidate_file.is_file():
        return dotted_name
    # Candidate 2: foo/bar/__init__.py → dotted mod "foo.bar"
    candidate_pkg = root.joinpath(*parts) / "__init__.py"
    if candidate_pkg.is_file():
        return dotted_name
    return None


class ASTImportScanner:
    """Scan a source tree for import edges using the ast module."""

    def scan(self, root: Path) -> ModelImportGraph:
        py_files = sorted(root.rglob("*.py"))
        # Map stem name → relative path string for each discovered file
        module_paths: dict[str, str] = {}
        for f in py_files:
            mod = _module_name(f, root)
            module_paths[mod] = str(f.relative_to(root))

        # Build edge list: (importer_mod, importee_mod) where importee is local.
        # OMN-11046: use exact Path-based resolution instead of prefix matching to
        # avoid the O(files x imports x modules) cross-product that produced ~2M edges.
        edges: list[tuple[str, str]] = []
        # Track which modules have at least one inbound edge
        has_inbound: set[str] = set()

        for f in py_files:
            importer = _module_name(f, root)
            try:
                source = f.read_text(encoding="utf-8")
            except OSError:
                continue
            for dotted_name in _extract_imports(source):
                resolved = _resolve_import(dotted_name, root)
                if resolved is not None and resolved in module_paths:
                    edges.append((importer, resolved))
                    has_inbound.add(resolved)

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
