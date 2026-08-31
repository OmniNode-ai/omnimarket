# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16804 AC4: no projection WRITE path may resolve tenant identity from a
map compiled into this source tree.

Detection is not enforcement (omni_home CLAUDE.md, operating rule 5). The
three-entry ``_LEGACY_TENANT_UUID_MAP`` was not a mistake anyone made once -- it
was the shape the write path was ASKED for, in a docstring that called itself
*"total over the closed set of values this codebase has ever written"*. Left
un-gated it regrows the moment someone adds a fourth entry to unblock a fourth
tenant, and the next tenant after that DLQs exactly as before.

This test scans the real AST of every handler under
``src/omnimarket/nodes/*/handlers/`` and fails closed on any reference to the
compiled map or to the resolver built on it. The single sanctioned reader is
``omnimarket/projection/tenant_registry_resolution``, which consults the map
ONLY after ``tenant_registry_mirror`` has already been asked and only for the
closed set of slugs written before the registry existed.

The map itself is deliberately NOT deleted: conversion migrations derive their
``USING`` clauses from ``resolve_tenant_uuid``, and a migration that cannot
express a slug must still refuse it. What is forbidden is a WRITE path reaching
for it -- a write path has a live database in hand and can ask the registry.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "omnimarket"
_NODES = _SRC / "nodes"

# The names that only exist because the source-compiled map exists.
_FORBIDDEN_NAMES = frozenset(
    {
        "_LEGACY_TENANT_UUID_MAP",
        "resolve_tenant_uuid",
        "resolve_tenant_uuid_or_none",
    }
)

# The one module allowed to read the closed legacy mapping, because it owns the
# ordering that makes it a fallback rather than an authority.
_SANCTIONED_READER = _SRC / "projection" / "tenant_registry_resolution.py"


def _handler_sources() -> list[Path]:
    return sorted(
        path
        for path in _NODES.glob("*/handlers/*.py")
        if path.name != "__init__.py" and path.resolve() != _SANCTIONED_READER
    )


def _forbidden_references(path: Path) -> list[tuple[int, str]]:
    """Every line in ``path`` that names a source-compiled tenant map symbol.

    AST-based, not grep-based: a prose mention of the name in a docstring or a
    comment is documentation, and this gate is about executable references. The
    OMN-16930 fence rationale and half a dozen handler comments legitimately
    name ``_LEGACY_TENANT_UUID_MAP`` while explaining why it is no longer used.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            hits.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            hits.append((node.lineno, node.attr))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _FORBIDDEN_NAMES:
                    hits.append((node.lineno, alias.name))
    return hits


@pytest.mark.unit
def test_the_scan_actually_sees_handlers() -> None:
    """A gate that scans nothing passes vacuously; prove the corpus is real."""
    sources = _handler_sources()
    assert len(sources) > 20, f"handler corpus looks wrong: {len(sources)} files"
    assert any(path.name == "handler_delegation.py" for path in sources), (
        "the live delegation write path must be in scope"
    )


@pytest.mark.unit
def test_no_write_path_resolves_tenant_identity_from_a_source_compiled_map() -> None:
    offenders: list[str] = []
    for path in _handler_sources():
        for lineno, name in _forbidden_references(path):
            offenders.append(f"{path.relative_to(_SRC.parent.parent)}:{lineno}: {name}")

    assert not offenders, (
        "OMN-16804 AC4: a projection write path resolves tenant identity from a "
        "map compiled into this source tree. Such a map cannot track a registry "
        "that gains a tenant on every beta signup, so every tenant outside it is "
        "quarantined instead of projected.\n\n"
        + "\n".join(offenders)
        + "\n\nResolve through omnimarket.projection.tenant_registry_resolution "
        "instead -- it asks tenant_registry_mirror (materialized by "
        "node_projection_tenant_registry from onex.tenant.events) first, and "
        "falls back to the closed legacy mapping only on a lane that has not "
        "applied the registry migration yet. Adding an entry to the map is NOT "
        "the fix; it is the defect, one tenant later."
    )


@pytest.mark.unit
def test_the_sanctioned_reader_is_the_only_one_and_still_orders_correctly() -> None:
    """The carve-out is narrow and is not itself a compiled-map resolver.

    ``tenant_registry_resolution`` may read the legacy mapping, but only through
    its own ``_legacy_tenant_uuid`` helper and only after the registry answer has
    been taken as a parameter. If the module ever calls ``resolve_tenant_uuid``
    directly it has become the thing it replaced.
    """
    source = _SANCTIONED_READER.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_SANCTIONED_READER))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "resolve_tenant_uuid" not in called
    assert "resolve_tenant_uuid_or_none" not in called
    assert "registry_uuid" in source, (
        "the registry answer must be an explicit input to the decision, not "
        "something the module reaches for on its own"
    )
