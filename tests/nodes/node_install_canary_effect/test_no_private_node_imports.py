# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install canary reads the file-list comparison from a shared package, not from a sibling node (OMN-22955).

The canary and the superset guard both compare the file list a commit installs with the install's copy. The canary used to
import ``file_list_comparison`` and ``resolve`` from the superset guard's private handler module, which made that node's
internal layout a cross-node dependency (the repository rule: do not make one node import another node's private handler or
model package; promote shared code instead). Golden: the canary imports no other node, the shared module holds the one
comparison, and both nodes use it. Error (the failing control): a sibling-node import is found by the same scan.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from omnimarket.nodes.node_install_canary_effect.handlers import handler_install_canary
from omnimarket.nodes.node_install_superset_guard_effect.handlers import (
    handler_install_superset,
)

pytestmark = pytest.mark.unit

NODE = "node_install_canary_effect"
NODE_ROOT = Path(handler_install_canary.__file__).resolve().parents[1]


def sibling_node_imports(source: str, own_node: str) -> list[str]:
    """Each module the source imports from ``omnimarket.nodes.<another node>``."""
    found: list[str] = []
    for tree_node in ast.walk(ast.parse(source)):
        modules: list[str] = []
        if isinstance(tree_node, ast.ImportFrom) and tree_node.module:
            modules.append(tree_node.module)
        elif isinstance(tree_node, ast.Import):
            modules.extend(alias.name for alias in tree_node.names)
        for module in modules:
            parts = module.split(".")
            if parts[:2] == ["omnimarket", "nodes"] and parts[2:3] not in (
                [],
                [own_node],
            ):
                found.append(module)
    return found


def test_golden_the_canary_imports_no_sibling_node() -> None:
    offenders = {
        str(path.relative_to(NODE_ROOT)): hits
        for path in sorted(NODE_ROOT.rglob("*.py"))
        if "tests" not in path.relative_to(NODE_ROOT).parts
        if (hits := sibling_node_imports(path.read_text(encoding="utf-8"), NODE))
    }
    assert offenders == {}


def test_golden_both_nodes_use_the_one_shared_comparison() -> None:
    install_file_list = importlib.import_module("omnimarket.handlers.install_file_list")
    assert (
        handler_install_canary.file_list_comparison
        is install_file_list.file_list_comparison
    )
    assert handler_install_canary.resolve is install_file_list.resolve
    assert (
        handler_install_superset.file_list_comparison
        is install_file_list.file_list_comparison
    )
    assert handler_install_superset.resolve is install_file_list.resolve


def test_error_the_scan_finds_a_sibling_node_import() -> None:
    private = (
        "from omnimarket.nodes.node_install_superset_guard_effect.handlers."
        "handler_install_superset import resolve\n"
    )
    own = "from omnimarket.nodes.node_install_canary_effect.models import X\n"
    shared = "from omnimarket.handlers.install_file_list import resolve\n"
    assert sibling_node_imports(private, NODE) == [
        "omnimarket.nodes.node_install_superset_guard_effect.handlers."
        "handler_install_superset"
    ]
    assert sibling_node_imports(own, NODE) == []
    assert sibling_node_imports(shared, NODE) == []
