# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Find the imports of a store implementation in a handler or reader module (the module's own source text)."""

from __future__ import annotations

import ast

_IMPLEMENTATION_PACKAGE = "omnimarket.handlers.overlay_store"
_CLIENT_LIBRARIES = frozenset({"redis", "valkey", "asyncpg", "psycopg", "aiokafka"})
_IMPLEMENTATION_PREFIXES = ("store_", "backend_")


def _implementation_module(name: str) -> bool:
    parts = name.split(".")
    if parts[0] in _CLIENT_LIBRARIES:
        return True
    package = _IMPLEMENTATION_PACKAGE.split(".")
    if parts[: len(package)] != package or len(parts) <= len(package):
        return False
    leaf = parts[len(package)]
    return leaf.startswith(_IMPLEMENTATION_PREFIXES) and leaf not in _ALLOWED_LEAVES


# The wiring and the registry are how a process picks a kind, so they are the modules a caller may import.
_ALLOWED_LEAVES = frozenset(
    {
        "store_wiring",
        "store_backend_registry",
        "store_import_check",
        "store_address_check",
    }
)


def forbidden_store_imports(source: str) -> list[str]:
    """The imported module names in ``source`` that name a store implementation or a store client library."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if _implementation_module(a.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _implementation_module(node.module):
                found.append(node.module)
            elif node.module == _IMPLEMENTATION_PACKAGE:
                found.extend(
                    f"{node.module}.{a.name}"
                    for a in node.names
                    if _implementation_module(f"{node.module}.{a.name}")
                )
    return found


__all__ = ["forbidden_store_imports"]
