# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A machine's free slots are the figure the tick's admission reads, and a machine it refuses is refused, not free (OMN-21733)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorHostRow,
    ModelFloorMachineDecl,
    ModelFloorPlacedHost,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    parse_pool_reading,
)
from tests.nodes.node_factory_floor_compute.support import ago, request, row

pytestmark = pytest.mark.unit

READ_AT = datetime(2026, 11, 2, 5, 36, 50, tzinfo=UTC)

# The pool read's line shapes, as the placement prints them (`Reading.line()`): a launching-host line carries
# lane_cap and lane_slots; a lab-host line carries cap, slots and placed only; a refused line ends with the refusal.
LANE_LINE = "hA:cores=24,load1=9.0,busy=8.0,free_gb=98.7,placed=0,cap=12,slots=9,login=claude,lane_cap=12,lane_slots=7"
RAW_LINE = (
    "hB:cores=32,load1=3.0,busy=2.0,free_gb=52.5,placed=0,cap=7,slots=2,login=claude"
)
CPU_LINE = (
    "hC:cores=32,load1=3.0,busy=2.0,free_gb=12.3,placed=0,cap=12,slots=0,login=claude,"
    "ADMISSION-REFUSED(CPU-IDLE-UNDER-BAR: 15.4% idle < 25%; RESERVE-MEMORY-FLOOR: 12.3G available leaves under the 10G floor)"
)
PARITY_LINE = (
    "hD:cores=16,load1=1.0,busy=1.0,free_gb=40.0,placed=0,cap=3,slots=3,login=claude,"
    "ADMISSION-REFUSED(PARITY-FAILED: plugin-stale:onex-overlays: installed plugin ships 575a24fd2ce4)"
)
LANE_REFUSED_LINE = (
    "hE:cores=24,load1=19.9,busy=18.0,free_gb=98.7,placed=0,cap=12,slots=0,login=claude,lane_cap=12,lane_slots=0,"
    "LANE-REFUSED(CPU-IDLE-UNDER-BAR: 25.0% idle < 25%)"
)
UNITS_LINE = (
    "hF:cores=32,load1=3.0,busy=2.0,free_gb=52.5,placed=0,cap=4,slots=4,login=claude"
)

LINES = (LANE_LINE, RAW_LINE, CPU_LINE, PARITY_LINE, LANE_REFUSED_LINE, UNITS_LINE)
HOSTS = ("hA", "hB", "hC", "hD", "hE", "hF")


def claim_rows(host: str, lanes: int) -> tuple[Any, ...]:
    return tuple(
        row(
            "CLAIM",
            60,
            f"{host}-lane-{i}",
            tickets=("OMN-5",),
            actor="claude:sonnet:workflow-lane",
            model="claude-sonnet",
            text="Work.",
            host=host,
        )
        for i in range(lanes)
    )


def machines() -> dict[str, Any]:
    result = compute_factory_floor(
        request(
            machines=tuple(ModelFloorMachineDecl(host=h, label=h) for h in HOSTS),
            pool=tuple(parse_pool_reading(line, ago(2)) for line in LINES),
            host_table=(
                ModelFloorHostRow(host="hA", cap=12),
                ModelFloorHostRow(host="hB", cap=7),
                ModelFloorHostRow(host="hC", cap=12),
                ModelFloorHostRow(host="hD", cap=3),
                ModelFloorHostRow(host="hE", cap=12),
                ModelFloorHostRow(host="hF", cap=4),
            ),
            # hF: 1 lane in the log but 3 placed units (unit work beside the lane): the cap counts units.
            placed=(
                ModelFloorPlacedHost(host="hA", units=3, lanes=3),
                ModelFloorPlacedHost(host="hB", units=4, lanes=4),
                ModelFloorPlacedHost(host="hC", units=6, lanes=6),
                ModelFloorPlacedHost(host="hD", units=0, lanes=0),
                ModelFloorPlacedHost(host="hE", units=0, lanes=0),
                ModelFloorPlacedHost(host="hF", units=3, lanes=1),
            ),
            ledger=claim_rows("hA", 4)
            + claim_rows("hB", 4)
            + claim_rows("hC", 6)
            + claim_rows("hF", 1),
        )
    )
    return {m.host: m for m in result.floor.machines}


def test_the_parser_keeps_the_lane_slots_cell_apart_from_the_raw_slots_cell() -> None:
    assert parse_pool_reading(LANE_LINE, READ_AT).lane_slots == 7
    assert parse_pool_reading(RAW_LINE, READ_AT).lane_slots is None
    assert parse_pool_reading(LANE_REFUSED_LINE, READ_AT).lane_slots == 0


def test_a_line_that_carries_lane_slots_is_drawn_with_the_readings_own_free_count() -> (
    None
):
    bay = machines()["hA"]
    assert bay.free_slots == 7  # the reading's lane_slots, not cap 12 - 4 lanes = 8
    assert (bay.admission.state, bay.admission.reason) == ("open", "")


def test_a_line_with_only_cap_slots_and_placed_is_cap_minus_units_not_lanes() -> None:
    bay = machines()["hB"]
    assert bay.free_slots == 3  # 7 - 4 units


def test_more_units_than_lanes_leaves_fewer_free_slots_than_cap_minus_lanes() -> None:
    bay = machines()["hF"]
    assert bay.lanes_placed == 1
    assert bay.free_slots == 1  # 4 - 3 units; the lane count alone would say 3


def test_an_admission_refused_host_with_free_raw_capacity_has_no_free_slots() -> None:
    bay = machines()["hC"]
    assert bay.free_slots == 0
    assert bay.admission.state == "refused"
    assert bay.admission.reason.startswith("CPU-IDLE-UNDER-BAR")
    assert "RESERVE-MEMORY-FLOOR" in bay.admission.reason


def test_a_parity_failed_host_is_refused_with_its_reason_not_drawn_free() -> None:
    bay = machines()["hD"]
    assert bay.free_slots == 0
    assert bay.admission.state == "refused"
    assert bay.admission.reason.startswith("PARITY-FAILED: plugin-stale:onex-overlays")


def test_a_lane_refused_host_has_no_free_slots_whatever_its_cell_says() -> None:
    bay = machines()["hE"]
    assert (bay.free_slots, bay.admission.state) == (0, "refused")
    assert bay.admission.reason == "CPU-IDLE-UNDER-BAR: 25.0% idle < 25%"


def test_free_slots_are_never_negative_when_units_exceed_the_cap() -> None:
    result = compute_factory_floor(
        request(
            machines=(ModelFloorMachineDecl(host="hB", label="hB"),),
            pool=(parse_pool_reading(RAW_LINE, ago(2)),),
            host_table=(ModelFloorHostRow(host="hB", cap=7),),
            placed=(ModelFloorPlacedHost(host="hB", units=9, lanes=2),),
        )
    )
    bay = result.floor.machines[0]
    assert (bay.free_slots, bay.admission.state) == (0, "open")


def test_an_unread_machine_is_neither_open_nor_free() -> None:
    result = compute_factory_floor(
        request(machines=(ModelFloorMachineDecl(host="hB", label="hB"),))
    )
    bay = result.floor.machines[0]
    assert (bay.free_slots, bay.admission.state) == (0, "unread")
