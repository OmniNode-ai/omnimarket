# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane each scenario is read under is the lane its node writes under (OMN-23171).

The green-days reader asks the ledger for each scenario's rows under the lane the days contract names. A scenario node writes its rows
under the lane its own contract names. When the two differ the reader finds no rows and the scenario reads red forever. R4 and R6 write under
``node_delegation_scenario_r4_effect`` and ``node_delegation_scenario_r6_effect`` while the list named ``..._r4`` and ``..._r6``.

The named break is the old list: the check run over it reports exactly R4 and R6.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_delegation_scenario_days_effect.handlers.handler_scenario_days_run import (
    load_runtime,
)

pytestmark = pytest.mark.unit

NODES_DIR = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
NODE_NAME = re.compile(r"^node_delegation_scenario_r(\d)_effect$")

# The lanes the R4 and R6 nodes write their rows under, as their own contracts name them (runtime.lane).
WRITER_LANES = {
    "R4": "node_delegation_scenario_r4_effect",
    "R6": "node_delegation_scenario_r6_effect",
}


def written_lanes() -> dict[str, str]:
    """Scenario id -> the lane that scenario's node contract in this tree writes under."""
    lanes: dict[str, str] = {}
    for node_dir in sorted(NODES_DIR.iterdir()):
        match = NODE_NAME.match(node_dir.name)
        if match is None:
            continue
        contract = yaml.safe_load((node_dir / "contract.yaml").read_text())
        lanes[f"R{match.group(1)}"] = str(contract["runtime"]["lane"])
    return lanes


def lane_mismatches(
    read_lanes: dict[str, str], writer_lanes: dict[str, str]
) -> dict[str, tuple[str, str]]:
    """Scenario id -> (lane read, lane written) for every scenario whose two lanes differ."""
    return {
        scenario_id: (read_lanes[scenario_id], lane)
        for scenario_id, lane in writer_lanes.items()
        if read_lanes.get(scenario_id) != lane
    }


def read_lanes() -> dict[str, str]:
    return {s.scenario_id: s.lane for s in load_runtime().scenarios}


def test_every_scenario_node_in_the_tree_is_read_under_the_lane_it_writes() -> None:
    written = written_lanes()
    assert written, "no scenario node found: the discovery reads nothing"
    assert lane_mismatches(read_lanes(), written) == {}


@pytest.mark.parametrize("scenario_id", sorted(WRITER_LANES))
def test_r4_and_r6_are_read_under_their_writer_lane(scenario_id: str) -> None:
    assert read_lanes()[scenario_id] == WRITER_LANES[scenario_id]


def test_named_break_the_old_list_is_reported_as_r4_and_r6() -> None:
    old = {
        **read_lanes(),
        "R4": "node_delegation_scenario_r4",
        "R6": "node_delegation_scenario_r6",
    }
    assert lane_mismatches(old, WRITER_LANES) == {
        "R4": ("node_delegation_scenario_r4", "node_delegation_scenario_r4_effect"),
        "R6": ("node_delegation_scenario_r6", "node_delegation_scenario_r6_effect"),
    }
