# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A red row whose owner ticket has a running lane shows that lane (OMN-22994).

On 2026-11-07 at 12:46Z every red row read ``lanes []`` while dozens of lanes ran. The lanes were on child tickets of
the declared owner (the CLAIM names the child in ``ticket=`` and the owner in ``parent_ticket=``), and the floor read
only ``ticket=``. The chain below runs the real readers end to end: ledger text, the floor record, the record's lanes,
the board. The control is the same ledger with the ``parent_ticket=`` cell taken away, which reads ``lanes []``.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import parse_ledger_text
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    load_record,
    record_lanes,
)
from tests.nodes.node_factory_floor_compute.support import NOW, request
from tests.nodes.node_floor_simple_compute.test_unowned_red_floor_omn22862 import (
    OWNERS,
    floor_doc,
)

pytestmark = pytest.mark.unit

UMBRELLA = "OMN-22478"


def _stamp(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger(parent_cell: str) -> str:
    # The ledger reader takes grep output: ``L<line number> <row>``.
    rows = [
        f"{_stamp(30)} | CLAIM | lane=rb-one | principal=p | ticket=OMN-22901 {parent_cell}| actor=a | model=m | host=h053 | run=r1 | text",
        f"{_stamp(20)} | CLAIM | lane=rb-two | principal=p | ticket=OMN-22902 {parent_cell}| actor=a | model=m | host=h101 | run=r2 | text",
        f"{_stamp(25)} | CLAIM | lane=rb-done | principal=p | ticket=OMN-22903 {parent_cell}| actor=a | model=m | host=h101 | run=r3 | text",
        f"{_stamp(5)} | TERMINAL | lane=rb-done | ticket=OMN-22903 | outcome=done | closes-CLAIM={_stamp(25)}",
        f"{_stamp(4)} | STATUS | lane=rb-one | ticket=OMN-22901 | working",
        f"{_stamp(3)} | STATUS | lane=rb-two | ticket=OMN-22902 | working",
    ]
    return "\n".join(f"L{n} {row}" for n, row in enumerate(rows, 1))


def _lanes_on_the_board(parent_cell: str) -> tuple[tuple[str, ...], dict[str, object]]:
    result = compute_factory_floor(
        request(ledger=parse_ledger_text(_ledger(parent_cell)))
    )
    live = tuple(sorted(lane.lane for lane in result.floor.lanes))
    record = load_record(json.dumps(result.model_dump(mode="json")).encode())
    owners = {**OWNERS, "stoppage:lab-idle": (UMBRELLA,)}
    doc = floor_doc(owners, record_lanes(record))
    row = next(c for c in doc.causes if c.id == "stoppage:lab-idle")
    return live, {
        "state": row.state,
        "red_with_work": doc.red_rule.red_with_work_under_way,
    }


def test_golden_the_record_lists_every_claim_without_a_terminal() -> None:
    live, _ = _lanes_on_the_board(f"| parent_ticket={UMBRELLA} ")
    assert live == ("rb-one", "rb-two")


def test_golden_a_red_row_whose_owner_ticket_has_a_running_child_lane_shows_it() -> (
    None
):
    _, board = _lanes_on_the_board(f"| parent_ticket={UMBRELLA} ")
    assert board["state"] == "amber"


def test_error_control_without_the_parent_cell_the_row_stays_red_with_no_lane() -> None:
    # Tonight's read: the lanes run (the record lists them) but nothing ties them to the row's owner.
    live, board = _lanes_on_the_board("")
    assert live == ("rb-one", "rb-two")
    assert board["state"] == "red"
