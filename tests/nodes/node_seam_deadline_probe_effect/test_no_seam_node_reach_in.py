# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handlers and models of the three seam nodes import no other node's handlers or models package.

CLAUDE.md: one node does not import another node's private handler or model package. A handler takes
the other node's handler through a protocol; the runtime module is the composition root that wires it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_SEAM_NODES = (
    "node_seam_deadline_probe_effect",
    "node_seam_read_orchestrator",
    "node_seam_reading_compute",
)
_PRIVATE = ("handlers", "models")


def _reach_ins(node: str) -> list[str]:
    found: list[str] = []
    for package in _PRIVATE:
        for path in sorted((_NODES / node / package).rglob("*.py")):
            found.extend(_scan(node, path))
    return found


def _scan(node: str, path: Path) -> list[str]:
    found: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for stmt in ast.walk(tree):
        if not isinstance(stmt, ast.ImportFrom) or not stmt.module:
            continue
        parts = stmt.module.split(".")
        if parts[:2] != ["omnimarket", "nodes"] or len(parts) < 4:
            continue
        owner, package = parts[2], parts[3]
        if owner in _SEAM_NODES and owner != node and package in _PRIVATE:
            found.append(f"{path.name}: from {stmt.module}")
    return found


@pytest.mark.parametrize("node", _SEAM_NODES)
def test_seam_node_reaches_into_no_other_seam_node(node: str) -> None:
    assert _reach_ins(node) == []
