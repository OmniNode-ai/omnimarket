# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20169 AC7: no configuration is chosen or tuned on a live request.

The configuration grid runs offline. Nothing outside the capability map's own
handlers imports it, so no live path can search configurations, and the best
configuration is a read of stored cells alone.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from omnimarket.models.delegation_capability_map.model_capability_cell import (
    ModelCapabilityCell,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers import (
    best_configurations,
)

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
_GRID_MODULE = "omnimarket.nodes.node_projection_delegation_capability_map.handlers.handler_capability_grid_run"
_GRID_NAMES = {"HandlerCapabilityGridRun", "ProtocolCapabilityGridGrader"}
_MAP_PACKAGE = _SRC / "nodes" / "node_projection_delegation_capability_map"


def _grid_importers() -> list[str]:
    importers: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if _MAP_PACKAGE in path.parents and path.name != "lab_run.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names = {alias.name for alias in node.names}
                if node.module == _GRID_MODULE or (
                    node.module.startswith(
                        "omnimarket.nodes.node_projection_delegation_capability_map"
                    )
                    and names & _GRID_NAMES
                ):
                    importers.append(str(path.relative_to(_SRC)))
            elif isinstance(node, ast.Import):
                if any(alias.name == _GRID_MODULE for alias in node.names):
                    importers.append(str(path.relative_to(_SRC)))
    return importers


def test_no_live_configuration_search_nothing_outside_the_map_handlers_imports_the_grid() -> (
    None
):
    assert _grid_importers() == []


def test_no_live_configuration_search_the_best_configuration_reads_stored_cells_only() -> (
    None
):
    parameters = inspect.signature(best_configurations).parameters
    assert list(parameters) == ["cells"]
    source = inspect.getsource(best_configurations)
    for forbidden in ("grader", "dispatch", "request", "prompt", "time.", "datetime"):
        assert forbidden not in source
    assert best_configurations(()) == ()
    assert ModelCapabilityCell.model_fields["verdict"]
