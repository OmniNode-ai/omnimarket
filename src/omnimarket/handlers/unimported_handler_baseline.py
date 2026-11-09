# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unimported-handler scan and its shrink-only baseline (OMN-10821, OMN-23335).

A ``Handler*`` class defined in ``handler_*.py`` that is never referenced from a
file other than its own cannot be invoked by the DI container, plugin system or
consumer. ``find_dead_handlers`` lists those not recorded in the baseline;
``find_stale_baseline_entries`` lists baseline entries that no longer name such a
class (the file or class is gone, or the class is now imported), so the
shrink-only baseline cannot hide a later regression of the same name. The CI
script ``scripts/ci/check_unimported_handlers.py`` is the command line.
"""

from __future__ import annotations

import ast
import contextlib
import pathlib
import re

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
    repo_root = src_root.parent.parent
    baseline = _load_baseline(baseline_path)

    dead: list[str] = []
    for class_name, defining_file, lineno in sorted(definitions):
        if _is_imported_elsewhere(class_name, defining_file, all_source_symbols):
            continue
        rel = _repo_relative(defining_file, repo_root)
        baseline_key = f"{rel}:{class_name}"
        if baseline_key in baseline:
            continue
        dead.append(f"{rel}:{lineno}: {class_name}")

    return dead


def _is_imported_elsewhere(
    class_name: str,
    defining_file: pathlib.Path,
    all_source_symbols: dict[pathlib.Path, set[str]],
) -> bool:
    """Return True when a file other than the defining one references the class."""
    return any(
        class_name in symbols
        for py_file, symbols in all_source_symbols.items()
        if py_file != defining_file
    )


def find_stale_baseline_entries(
    src_root: pathlib.Path,
    baseline_path: pathlib.Path | None,
) -> list[str]:
    """Return sorted baseline keys that no longer name a dead Handler* class.

    A baseline entry is stale when its file or class is gone, or when the class is
    now imported elsewhere. The baseline is shrink-only, so a stale entry must be
    removed rather than left to hide a future regression of the same name.
    """
    baseline = _load_baseline(baseline_path)
    if not baseline:
        return []

    repo_root = src_root.parent.parent
    all_sources = _collect_all_source_lines(src_root)
    all_source_symbols = {
        py_file: _collect_referenced_symbols(source)
        for py_file, source in all_sources.items()
    }
    still_dead = {
        f"{_repo_relative(defining_file, repo_root)}:{class_name}"
        for class_name, defining_file, _lineno in _collect_handler_definitions(src_root)
        if not _is_imported_elsewhere(class_name, defining_file, all_source_symbols)
    }
    return sorted(baseline - still_dead)
