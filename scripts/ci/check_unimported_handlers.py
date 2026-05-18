#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI gate: detect Handler* classes with zero import references (OMN-10821).

A Handler class defined in handler_*.py that is never imported anywhere outside
its own file cannot be invoked by the DI container, plugin system, or consumer.
These are dead handlers — if they are meant to be active, they are silently absent
from the pipeline.

This check scans src/omnimarket/nodes/ for Handler* class definitions, then
verifies that each class is imported at least once in a file other than the one
that defines it. Files with an inline ``# unimported-handler-allow: <reason>``
annotation are skipped.

Exit codes: 0 = clean; 1 = dead handlers found; 2 = invocation error.
"""

from __future__ import annotations

import ast
import contextlib
import pathlib
import re
import sys

_INLINE_ALLOW_MARKER = "# unimported-handler-allow:"
_HANDLER_CLASS_RE = re.compile(r"^Handler[A-Z]\w*$")


def _collect_handler_definitions(
    src_root: pathlib.Path,
) -> dict[str, tuple[pathlib.Path, int]]:
    """Return {ClassName: (defining_file, lineno)} for all Handler* classes."""
    definitions: dict[str, tuple[pathlib.Path, int]] = {}
    for py_file in sorted(src_root.rglob("handler_*.py")):
        # Skip test files
        if py_file.name.startswith("test_") or "conftest" in py_file.name:
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if _INLINE_ALLOW_MARKER in source:
            continue
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _HANDLER_CLASS_RE.match(node.name):
                definitions[node.name] = (py_file, node.lineno)
    return definitions


def _collect_all_source_lines(src_root: pathlib.Path) -> dict[pathlib.Path, str]:
    """Return {path: source_text} for all .py files under src_root."""
    sources: dict[pathlib.Path, str] = {}
    for py_file in sorted(src_root.rglob("*.py")):
        with contextlib.suppress(OSError):
            sources[py_file] = py_file.read_text(encoding="utf-8")
    return sources


def find_dead_handlers(src_root: pathlib.Path) -> list[str]:
    """Return sorted list of violation strings for Handler* with zero external imports."""
    definitions = _collect_handler_definitions(src_root)
    if not definitions:
        return []

    all_sources = _collect_all_source_lines(src_root)

    dead: list[str] = []
    for class_name, (defining_file, lineno) in sorted(definitions.items()):
        imported_elsewhere = False
        for py_file, source in all_sources.items():
            if py_file == defining_file:
                continue
            # Match: "import HandlerFoo" or "HandlerFoo," or "(HandlerFoo" etc.
            # A simple name occurrence in another file indicates it is referenced.
            if class_name in source:
                imported_elsewhere = True
                break
        if not imported_elsewhere:
            rel = defining_file.relative_to(src_root.parent.parent)
            dead.append(f"{rel}:{lineno}: {class_name}")

    return dead


def main() -> int:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    src_root = repo_root / "src" / "omnimarket"
    if not src_root.is_dir():
        print(f"ERROR: src/omnimarket not found under {repo_root}", file=sys.stderr)
        return 2

    dead = find_dead_handlers(src_root)
    if dead:
        print(
            f"ERROR: {len(dead)} Handler class(es) with zero external import references:"
        )
        for entry in dead:
            print(f"  {entry}")
        print()
        print(
            "Fix: import the handler in the node's __init__.py or registry module so the "
            "DI container can discover and wire it. If the handler is intentionally "
            "unreferenced (e.g. a base class or stub), add an inline "
            "`# unimported-handler-allow: <reason>` annotation to the file."
        )
        return 1

    print(
        "OK: all Handler* classes in src/omnimarket/nodes/ are referenced externally."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
