# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The nodes the enforcement plan names as existing resolve to node directories with contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
# The consumer or carrier of each of the four mechanisms, as the plan's mechanism table names them.
NAMED_EXISTING = (
    "node_third_lease_stop_compute",
    "node_andon_stop_reducer",
    "node_pr_handoff_decision_compute",
    "node_two_strike_arbiter",
    "node_wip_admission_compute",
    "node_wip_gate_reducer",
    "node_idle_slot_fill_effect",
)


@pytest.mark.parametrize("node", NAMED_EXISTING)
def test_third_lease_stop_plan_names_resolve(node: str) -> None:
    contract = NODES / node / "contract.yaml"
    assert contract.is_file(), node
    assert yaml.safe_load(contract.read_text(encoding="utf-8"))["name"].endswith(
        node.removeprefix("node_")
    )
