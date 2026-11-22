# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The done-items nodes import shared owners, not another node's package (OMN-25104).

- test_done_items_nodes_import_no_other_node_package: a done-items node imports
  another node's private handler or model package (omnimarket CLAUDE.md: promote
  shared types instead).
- test_ledger_row_parser_owner_is_shared: the ledger row parser is still defined
  in a node's private handler module instead of the shared handlers package.
- test_old_row_parser_path_is_the_same_objects: a copy instead of a move makes
  two LedgerRowRefusalError classes, so an except clause in an importer still on
  the old path stops catching the shared parser's refusal.
- test_done_items_fold_owner_is_shared: the done-items fold is still defined in
  the compute node's private handler module instead of the shared handlers package.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_PREFIX = "omnimarket.nodes."


def _absolute_imports(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


def test_done_items_nodes_import_no_other_node_package() -> None:
    violations: list[str] = []
    for node_dir in (
        _NODES / "node_projection_done_items",
        _NODES / "node_done_items_compute",
    ):
        for py in sorted(node_dir.rglob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for name in _absolute_imports(tree):
                if name.startswith(_PREFIX) and name.split(".")[2] != node_dir.name:
                    violations.append(f"{py.relative_to(_NODES)}: {name}")
    assert violations == []


def test_ledger_row_parser_owner_is_shared() -> None:
    from omnimarket.handlers.ledger_row_parser import parse_ledger_row

    assert parse_ledger_row.__module__ == "omnimarket.handlers.ledger_row_parser"


def test_old_row_parser_path_is_the_same_objects() -> None:
    from omnimarket.handlers import ledger_row_parser as shared
    from omnimarket.nodes.node_work_ledger_emit_effect.handlers import (
        row_parser as old,
    )

    assert old.parse_ledger_row is shared.parse_ledger_row
    assert old.row_id_of is shared.row_id_of
    assert old.LedgerRowRefusalError is shared.LedgerRowRefusalError
    with pytest.raises(old.LedgerRowRefusalError):
        shared.parse_ledger_row("not a ledger row")


def test_done_items_fold_owner_is_shared() -> None:
    from omnimarket.handlers.done_items_fold import fold_done_items

    assert fold_done_items.__module__ == "omnimarket.handlers.done_items_fold"
