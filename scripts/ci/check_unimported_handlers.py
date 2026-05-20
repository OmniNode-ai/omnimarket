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
) -> list[tuple[str, pathlib.Path, int]]:
    """Return (ClassName, defining_file, lineno) for all Handler* classes."""
    definitions: list[tuple[str, pathlib.Path, int]] = []
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
                definitions.append((node.name, py_file, node.lineno))
    return definitions


def _collect_all_source_lines(src_root: pathlib.Path) -> dict[pathlib.Path, str]:
    """Return {path: source_text} for all .py files under src_root."""
    sources: dict[pathlib.Path, str] = {}
    for py_file in sorted(src_root.rglob("*.py")):
        with contextlib.suppress(OSError):
            sources[py_file] = py_file.read_text(encoding="utf-8")
    return sources


def _collect_contract_sources(src_root: pathlib.Path) -> dict[pathlib.Path, str]:
    """Return {path: source_text} for node contract files under src_root."""
    sources: dict[pathlib.Path, str] = {}
    for contract_file in sorted((src_root / "nodes").rglob("contract.yaml")):
        with contextlib.suppress(OSError):
            sources[contract_file] = contract_file.read_text(encoding="utf-8")
    return sources


def _repo_relative(path: pathlib.Path, repo_root: pathlib.Path) -> pathlib.Path:
    with contextlib.suppress(ValueError):
        return path.relative_to(repo_root)
    return path


def _load_baseline(baseline_path: pathlib.Path | None) -> set[str]:
    """Load documented legacy entries as ``repo/path.py:HandlerClass`` keys."""
    if baseline_path is None or not baseline_path.is_file():
        return set()

    entries: set[str] = set()
    for raw_line in baseline_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        entries.add(line.split("#", 1)[0].strip())
    return entries


def _collect_referenced_symbols(source: str) -> set[str]:
    """Return Python symbols imported or referenced by AST nodes."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                symbols.add(alias.asname or alias.name.rsplit(".", 1)[-1])
            continue

        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                symbols.add(alias.asname or alias.name)
            continue

        if isinstance(node, ast.Name):
            symbols.add(node.id)
            continue

        if isinstance(node, ast.Attribute):
            symbols.add(node.attr)

    return symbols


def find_dead_handlers(
    src_root: pathlib.Path,
    baseline_path: pathlib.Path | None = None,
) -> list[str]:
    """Return sorted violation strings for Handler* with no wiring evidence."""
    definitions = _collect_handler_definitions(src_root)
    if not definitions:
        return []

    all_sources = _collect_all_source_lines(src_root)
    all_source_symbols = {
        py_file: _collect_referenced_symbols(source)
        for py_file, source in all_sources.items()
    }
    contract_sources = _collect_contract_sources(src_root)
    repo_root = src_root.parent.parent
    baseline = _load_baseline(baseline_path)

    dead: list[str] = []
    for class_name, defining_file, lineno in sorted(definitions):
        imported_elsewhere = False
        for py_file, symbols in all_source_symbols.items():
            if py_file == defining_file:
                continue
            if class_name in symbols:
                imported_elsewhere = True
                break
        if not imported_elsewhere:
            contract_declared = any(
                class_name in source for source in contract_sources.values()
            )
            rel = _repo_relative(defining_file, repo_root)
            baseline_key = f"{rel}:{class_name}"
            if contract_declared or baseline_key in baseline:
                continue
            dead.append(f"{rel}:{lineno}: {class_name}")

    return dead


def main() -> int:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    src_root = repo_root / "src" / "omnimarket"
    baseline_path = (
        repo_root / "scripts" / "validation" / "unimported_handler_baseline.txt"
    )
    if not src_root.is_dir():
        print(f"ERROR: src/omnimarket not found under {repo_root}", file=sys.stderr)
        return 2

    dead = find_dead_handlers(src_root, baseline_path=baseline_path)
    if dead:
        print(f"ERROR: {len(dead)} Handler class(es) with no wiring evidence:")
        for entry in dead:
            print(f"  {entry}")
        print()
        print(
            "Fix: import the handler in a Python module, declare it in the node's "
            "contract.yaml handler/handler_routing metadata, or add a documented "
            "legacy entry to scripts/validation/unimported_handler_baseline.txt."
        )
        return 1

    print(
        "OK: all Handler* classes in src/omnimarket/nodes/ are imported, "
        "contract-declared, or explicitly baselined."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
