# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every live lane names a machine the floor draws or carries the cause that it names none (OMN-21930)."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import ModelFloorMachineDecl
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from tests.nodes.node_factory_floor_compute.support import request, row

pytestmark = pytest.mark.unit

MACHINES = (
    ModelFloorMachineDecl(host="h051", label="h051"),
    ModelFloorMachineDecl(host="h200", label="h200 (the Mac)"),
)


def claim(lane: str, **cells: Any) -> Any:
    return row("CLAIM", 10, lane, tickets=("OMN-5",), text=f"{lane} work", **cells)


def lanes_of(*rows: Any) -> dict[str, Any]:
    result = compute_factory_floor(request(machines=MACHINES, ledger=rows))
    return {lane.lane: lane for lane in result.floor.lanes}


def test_lane_machine_a_lane_with_a_drawn_host_has_no_cause() -> None:
    lane = lanes_of(claim("on-h051", host="h051"))["on-h051"]
    assert (lane.placement, lane.host, lane.placement_cause) == ("machine", "h051", "")


def test_lane_machine_a_lane_with_no_host_cell_is_listed_under_that_cause() -> None:
    lane = lanes_of(claim("dispatcher-1102"))["dispatcher-1102"]
    assert (lane.placement, lane.host) == ("unplaced", "")
    assert (lane.placement_cause, lane.named_host) == ("no-host-cell", "")


def test_lane_machine_a_host_the_floor_does_not_draw_is_listed_with_the_name_it_gave() -> (
    None
):
    lane = lanes_of(claim("on-201", host="h201-dev"))["on-201"]
    assert lane.placement == "unplaced"
    assert (lane.placement_cause, lane.named_host) == ("host-not-drawn", "h201-dev")


def test_lane_machine_every_live_lane_is_drawn_or_carries_a_cause() -> None:
    lanes = lanes_of(
        claim("a", host="h051"),
        claim("b"),
        claim("c", host="elsewhere"),
        claim("d", host="cloud-1"),
    )
    assert len(lanes) == 4
    for lane in lanes.values():
        assert (lane.placement == "unplaced") == (lane.placement_cause != "")
