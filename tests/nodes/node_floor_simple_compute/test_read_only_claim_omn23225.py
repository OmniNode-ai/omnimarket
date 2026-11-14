# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A read-only analysis CLAIM does not make a red row amber (OMN-23225).

At 2026-11-08T21:16:05Z the floor row ``stoppage:zero-flip-streak`` went from red to amber with ``work_under_way`` true
and ``lanes [whys-closer-2110]``, because that read-only five whys lane opened a CLAIM (21:15:03Z, ``worktree=none``) on
the row's owner ticket OMN-20655. The lane could change nothing, so a stoppage nobody was fixing read as owned. The chain
below runs the real readers end to end, as ``test_live_lane_child_ticket_omn22994`` does: ledger text, the floor record,
the record's lanes, the board. The positive control is the same CLAIM carrying a worktree, which is amber.
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

ROW = "stoppage:lab-idle"
TICKET = "OMN-20655"
WORKTREE = f"worktree=omni_worktrees/{TICKET}/omnimarket"


def _stamp(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger(claim_cells: str) -> str:
    # The ledger reader takes grep output: ``L<line number> <row>``.
    rows = [
        f"{_stamp(30)} | CLAIM | lane=whys-closer-2110 | principal=p | ticket={TICKET} | {claim_cells}"
        " | actor=a | model=m | host=h202 | run=r1 | five whys",
        f"{_stamp(4)} | STATUS | lane=whys-closer-2110 | ticket={TICKET} | working",
    ]
    return "\n".join(f"L{n} {row}" for n, row in enumerate(rows, 1))


def _board(claim_cells: str) -> dict[str, object]:
    result = compute_factory_floor(
        request(ledger=parse_ledger_text(_ledger(claim_cells)))
    )
    record = load_record(json.dumps(result.model_dump(mode="json")).encode())
    lanes = record_lanes(record)
    assert [lane.lane for lane in lanes] == ["whys-closer-2110"]
    doc = floor_doc({**OWNERS, ROW: (TICKET,)}, lanes)
    row = next(c for c in doc.causes if c.id == ROW)
    return {
        "state": row.state,
        "lanes": row.lanes,
        "work_under_way": row.work_under_way,
    }


def test_golden_a_claim_with_a_worktree_is_work_under_way() -> None:
    # The positive control: the same lane and ticket, with a worktree, turns the row amber and names the lane.
    board = _board(WORKTREE)
    assert board["state"] == "amber"
    assert board["work_under_way"] is True
    assert board["lanes"] == ("whys-closer-2110",)


def test_error_a_claim_with_worktree_none_leaves_the_row_red() -> None:
    # The recorded 21:15:03Z CLAIM: it names the owner ticket and no worktree.
    board = _board("worktree=none")
    assert board["state"] == "red"
    assert board["work_under_way"] is False
    assert board["lanes"] == ()


def test_error_a_claim_that_declares_mode_read_only_leaves_the_row_red() -> None:
    board = _board(f"{WORKTREE} | mode=read-only")
    assert board["state"] == "red"
    assert board["work_under_way"] is False
    assert board["lanes"] == ()


def test_golden_a_claim_that_names_no_worktree_cell_is_still_counted() -> None:
    # Absent is not declared: only an explicit none or read-only takes a lane out.
    board = _board("mode=build")
    assert board["state"] == "amber"
    assert board["lanes"] == ("whys-closer-2110",)
