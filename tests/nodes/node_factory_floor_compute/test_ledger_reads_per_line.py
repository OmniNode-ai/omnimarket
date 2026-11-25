# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The compute reads the ledger once, never once per line.

Measured on a lab host with the live ledger (187,000 rows after the window's reduction): 1,139 lines took 240 s in the stop scan
and 66 s in the task map, each line scanning every row for the few that name its tickets, so the record step's time grew with
lines times rows and passed the publisher's 45 s step limit. Row reads are counted here, not seconds, so the proof holds on any host.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorLedgerRow,
    ModelFloorManifest,
    ModelFloorPlanInput,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from tests.nodes.node_factory_floor_compute.support import (
    ago,
    config,
    request,
    task,
)

pytestmark = pytest.mark.unit

FILLER_ROWS = 1500
CELL_READS = [0]


class _CountedRow(ModelFloorLedgerRow):
    """A ledger row that counts each read of the two cells the per-line scans filter on."""

    def __getattribute__(self, name: str) -> Any:
        if name in ("kind", "tickets"):
            CELL_READS[0] += 1
        return super().__getattribute__(name)


def _plan(number: int) -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path=f"beta/plans/2026-10-31-line-{number}.md",
        title=f"Line {number}",
        tickets=(f"OMN-{number}",),
        manifest=ModelFloorManifest(
            title=f"Line {number} (tasks)",
            epic=f"OMN-{number}",
            tasks=(task("T1", "0", tickets=(f"OMN-{number}",)),),
        ),
    )


def _ledger(lines: int) -> tuple[ModelFloorLedgerRow, ...]:
    mapped = config().task_map_state
    on_lines = tuple(
        row
        for number in range(lines)
        for row in (
            _CountedRow(
                ts=ago(40),
                kind="TERMINAL",
                lane=f"lane-{number}",
                tickets=(f"OMN-{number}",),
                outcome="blocked",
            ),
            _CountedRow(
                ts=ago(30),
                kind="STATUS",
                lane=f"lane-{number}",
                tickets=(f"OMN-{number}",),
                state=mapped,
                task="T1",
                subject=_plan(number).plan_path,
            ),
        )
    )
    filler = tuple(
        _CountedRow(
            ts=ago(20 + position / 100),
            kind="STATUS",
            lane=f"filler-{position}",
            tickets=(f"OMN-9{position}",),
            state="note",
        )
        for position in range(FILLER_ROWS)
    )
    return on_lines + filler


def _cell_reads(lines: int) -> int:
    ledger = _ledger(lines)
    floor = request(
        plans=tuple(_plan(number) for number in range(lines)), ledger=ledger
    )
    CELL_READS[0] = 0
    result = compute_factory_floor(floor)
    assert len(result.lines) == lines
    return CELL_READS[0]


def test_reads_of_the_ledger_do_not_grow_with_the_number_of_lines() -> None:
    few = _cell_reads(4)
    many = _cell_reads(32)
    # A scan per line reads every row once per line: eight times the lines would be about eight times the reads.
    assert many < few * 2, (few, many)


def test_a_blocked_lane_on_a_line_ticket_still_stops_that_line_only() -> None:
    ledger = _ledger(3)
    floor = request(plans=tuple(_plan(number) for number in range(3)), ledger=ledger)
    result = compute_factory_floor(floor)
    assert [line.andon.flag for line in result.lines] == [True, True, True]
    assert all("ended blocked" in line.andon.reason for line in result.lines)
