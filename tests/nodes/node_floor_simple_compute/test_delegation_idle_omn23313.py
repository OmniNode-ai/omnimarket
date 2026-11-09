# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor draws a red delegation:idle row when an operator-owned delegation ticket can start and has no lane (OMN-23313).

On the night of 2026-11-08 to 11-09 no lane worked an operator-owned delegation ticket while lab slots sat free. The floor
read piece 15 as working, outside the pieces in scope, so nothing on the board went red and the board check, which works
red rows only, never picked delegation up. The row is red while any such ticket has no live lane (``live_lanes``: its own
ticket or a ``parent_ticket=``), amber when each has one, and absent when none can start. Which tickets count is overlay
data; the control for each reading is the same board with the row's ticket, lane or overlay block taken away.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorLane,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
)
from omnimarket.models.floor_17a.model_delegation_idle import (
    ModelDelegationTicketIn,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from tests.nodes.node_factory_floor_compute.support import NOW
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

ROW = "delegation:idle"
SPEC = {
    "epics": ["EPIC-1"],
    "operator_assignees": ["operator-1"],
    "title": "Operator-owned delegation tickets can start and have no lane",
}


def ticket(tid: str) -> ModelDelegationTicketIn:
    return ModelDelegationTicketIn(
        ticket=tid, title=f"ticket {tid}", assignee="operator-1"
    )


def lane(name: str, **kw: object) -> ModelFloorLane:
    return ModelFloorLane.model_validate({"lane": name, "liveness": "alive", **kw})


def board(
    tickets: tuple[ModelDelegationTicketIn, ...] | None,
    lanes: tuple[ModelFloorLane, ...] = (),
    *,
    spec: dict[str, Any] | None = SPEC,
    owners: dict[str, list[str]] | None = None,
    sources_read: dict[str, bool] | None = None,
) -> ModelFloorSimple:
    base = overlay_text("node_floor_simple_compute")
    causes = {**base["causes"], "owners": owners or {}}
    if spec is not None:
        causes["delegation_idle"] = spec
    base["causes"] = causes
    return HandlerFloorSimple().handle(
        ModelFloorSimpleInput(
            now=NOW,
            overlay=ModelFloorSimpleOverlay.model_validate(base),
            open_prs=10,
            open_prs_before=12,
            merges_in_window=4,
            watcher_read_at=NOW - timedelta(minutes=1),
            slots_working=5,
            slots_total=20,
            record_read_at=NOW - timedelta(minutes=1),
            lanes=lanes,
            lab=None,
            receipts=None,
            go_given_through=0,
            delegation_tickets=tickets,
            sources_read=sources_read or {},
        )
    )


def row(doc: ModelFloorSimple):
    found = [c for c in doc.causes if c.id == ROW]
    return found[0] if found else None


def test_golden_delegation_idle_is_red_and_names_each_ticket_with_no_lane() -> None:
    doc = board(
        (ticket("OMN-1"), ticket("OMN-2"), ticket("OMN-3")),
        (lane("a", ticket="OMN-1"),),
    )
    cause = row(doc)
    assert cause is not None, "the board has no delegation:idle row"
    assert cause.state == "red"
    assert cause.owners == ("OMN-2", "OMN-3")
    assert cause.tickets == ("OMN-1", "OMN-2", "OMN-3")
    assert "2 of 3" in cause.detail
    assert "OMN-2, OMN-3" in cause.detail
    assert cause.size == 2
    assert ROW in doc.red_rule.uncarried_red
    assert doc.red_rule.violations == ()


def test_golden_delegation_idle_turns_amber_when_each_ticket_has_a_live_lane() -> None:
    lanes = (lane("a", ticket="OMN-1"), lane("b", parent_ticket="OMN-2"))
    doc = board((ticket("OMN-1"), ticket("OMN-2")), lanes)
    cause = row(doc)
    assert cause is not None
    assert cause.state == "amber"
    assert cause.lanes == ("a", "b")
    assert doc.red_rule.violations == ()
    assert ROW not in doc.red_rule.uncarried_red


def test_error_delegation_idle_a_dead_or_stalled_lane_is_not_a_lane() -> None:
    lanes = (
        lane("dead", ticket="OMN-1", liveness="dead"),
        lane("done", ticket="OMN-1", liveness="finished"),
        lane("stalled", ticket="OMN-1", stalled=True),
    )
    cause = row(board((ticket("OMN-1"),), lanes))
    assert cause is not None
    assert cause.state == "red"
    assert cause.owners == ("OMN-1",)


def test_delegation_idle_owners_resolve_from_the_overlay_owners_map() -> None:
    owners = {ROW: ["OMN-23313"]}
    doc = board((ticket("OMN-1"),), (), owners=owners)
    cause = row(doc)
    assert cause is not None
    assert cause.state == "red"
    assert cause.owners == ("OMN-1", "OMN-23313")
    assert ROW not in doc.red_rule.unowned_red
    # Like every other row, a live lane on an owner the map names is work under way on the row.
    worked = board(
        (ticket("OMN-1"),), (lane("fix", ticket="OMN-23313"),), owners=owners
    )
    assert row(worked).state == "amber"
    assert worked.red_rule.violations == ()


def test_delegation_idle_with_no_owners_entry_is_still_owned_through_its_idle_tickets() -> (
    None
):
    # The row owns its idle tickets, so it is never an owner-less red row.
    assert ROW not in board((ticket("OMN-1"),)).red_rule.unowned_red


def test_delegation_idle_no_startable_ticket_draws_no_row() -> None:
    assert row(board(())) is None


def test_error_delegation_idle_an_unread_ticket_list_is_a_cause_of_its_own_not_an_empty_row() -> (
    None
):
    doc = board(None, sources_read={"delegation_tickets": False})
    assert row(doc) is None
    unread = [c for c in doc.causes if c.id == "source:delegation_tickets"]
    assert unread
    assert unread[0].state == "red"


def test_delegation_idle_overlay_without_the_block_draws_no_row() -> None:
    assert row(board((ticket("OMN-1"),), spec=None)) is None
