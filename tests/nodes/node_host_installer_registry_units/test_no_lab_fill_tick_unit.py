# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No unit declared here runs a lab fill copy; the removed fill nodes are absent (OMN-24579)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    load_units,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
REMOVED = (
    "node_lab_fill_effect",
    "node_lab_fill_selection_compute",
    "node_lab_fill_plan_compute",
    "node_idle_slot_fill_effect",
)


def test_lab_fill_tick_unit_is_not_declared() -> None:
    assert "lab-fill-tick" not in load_units()


@pytest.mark.parametrize("node", REMOVED)
def test_removed_fill_node_is_absent(node: str) -> None:
    assert not (NODES / node).exists()
