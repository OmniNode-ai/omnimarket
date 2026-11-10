# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily line effect reaches the compute node only through its public package (OMN-22925)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from omnimarket.nodes.node_daily_line_effect.handlers import handler_daily_line_run

pytestmark = pytest.mark.unit

COMPUTE_PACKAGE = "omnimarket.nodes.node_daily_line_compute"


def private_imports(source: str) -> list[str]:
    """Modules below another node's package that the source imports from."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(f"{COMPUTE_PACKAGE}."):
                found.append(node.module)
        elif isinstance(node, ast.Import):
            found.extend(
                alias.name
                for alias in node.names
                if alias.name.startswith(f"{COMPUTE_PACKAGE}.")
            )
    return found


def test_effect_handler_imports_nothing_below_the_compute_nodes_package() -> None:
    source = Path(handler_daily_line_run.__file__).read_text(encoding="utf-8")
    assert private_imports(source) == []


def test_control_a_private_handler_import_is_found() -> None:
    source = (
        f"from {COMPUTE_PACKAGE}.handlers.handler_daily_line import HandlerDailyLine\n"
    )
    assert private_imports(source) == [f"{COMPUTE_PACKAGE}.handlers.handler_daily_line"]
