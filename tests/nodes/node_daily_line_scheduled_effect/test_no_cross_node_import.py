# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily line scheduled effect reaches the other nodes only through their public packages (OMN-25163).

tests/nodes/node_daily_line_effect/test_no_cross_node_import.py covers the effect node's own import of the compute node
and nothing in this node; tests/test_no_cross_node_reach_in.py covers only imports of another node's ``models`` package.
This rule covers every module of this node and any other node's submodule, handlers included.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
OWN = "node_daily_line_scheduled_effect"
NODE_ROOT = REPO / "src/omnimarket/nodes" / OWN


def below_another_nodes_package(source: str, own: str = OWN) -> list[str]:
    """Modules below ``omnimarket.nodes.<other node>.`` that the source imports from (the package itself is public)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if (
                len(parts) > 3
                and parts[:2] == ["omnimarket", "nodes"]
                and parts[2] != own
            ):
                found.append(name)
    return found


def test_no_module_of_the_node_imports_below_another_nodes_package() -> None:
    sources = sorted(NODE_ROOT.rglob("*.py"))
    assert sources
    for path in sources:
        assert below_another_nodes_package(path.read_text("utf-8")) == [], path


def test_control_a_private_handler_import_of_another_node_is_found() -> None:
    source = (
        "from omnimarket.nodes.node_daily_line_compute.handlers.handler_daily_line import HandlerDailyLine\n"
        "import omnimarket.nodes.node_daily_line_effect.handlers.handler_daily_line_run\n"
        "from omnimarket.nodes.node_daily_line_compute import HandlerDailyLine as Public\n"
        f"from omnimarket.nodes.{OWN}.models import ModelDailyLineScheduleOverlay\n"
    )
    assert below_another_nodes_package(source) == [
        "omnimarket.nodes.node_daily_line_compute.handlers.handler_daily_line",
        "omnimarket.nodes.node_daily_line_effect.handlers.handler_daily_line_run",
    ]
