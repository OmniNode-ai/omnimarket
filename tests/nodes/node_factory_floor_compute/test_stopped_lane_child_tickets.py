# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane on a child of a goal root is in scope for the stopped line (OMN-22934).

The goal's ticket rule names roots; the work is minted under them, so a lane's own ticket is a root's child, or a
child of a child. The Linear snapshot's child ids lead from a root down to the lane's ticket. The rows are real rows of
the ledger of 2026-11-09: ``labrunpool-1050`` ended partial on OMN-23036 (a child of the root OMN-21288) naming a head
and a follow-up, and ``p9shell-2250`` ended partial on OMN-23393 with a row that names no branch, ask or next step.
Without the snapshot's children both read out of scope: that is what the floor listed on the live ledger before this.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.floor_17a.model_floor_17a import ModelGoalScope
from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorLinearIssue,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_stopped import (
    build_stopped,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    load_linear,
    merge_linear_titles,
    parse_ledger_text,
)
from tests.nodes.node_factory_floor_compute.support import request

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "stopped-lane-child-ticket-ledger-2026-11-09.txt"
)
READ_AT = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)
ROOT = "OMN-21288"
CHILD = "OMN-23036"
SCOPE = ModelGoalScope(ruling="test")


def issue(ticket: str, *children: str) -> ModelFloorLinearIssue:
    return ModelFloorLinearIssue(id=ticket, children=children)


def floor_request(
    linear: tuple[ModelFloorLinearIssue, ...] = (),
    roots: tuple[str, ...] = (ROOT,),
    depth: int | None = None,
) -> ModelFactoryFloorRequest:
    made = request(
        ledger=parse_ledger_text(FIXTURE.read_text(encoding="utf-8")),
        goal_scope=SCOPE,
        linear=linear,
        scope_ticket_roots=roots,
    )
    cfg = made.config
    if depth is not None:
        cfg = cfg.model_copy(
            update={
                "stopped": cfg.stopped.model_copy(update={"scope_max_depth": depth})
            }
        )
    return made.model_copy(update={"generated_at": READ_AT, "config": cfg})


def causes(request_: ModelFactoryFloorRequest) -> dict[str, str]:
    return {s.lane: s.cause for s in build_stopped(request_)[1]}


def test_golden_a_lane_on_a_child_of_a_root_is_listed() -> None:
    stopped, skipped, cause = build_stopped(floor_request((issue(ROOT, CHILD),)))
    assert cause == ""
    assert [s.lane for s in stopped] == ["labrunpool-1050"]
    assert stopped[0].ticket == CHILD
    assert stopped[0].names == ("next-step",)
    assert stopped[0].head == "75c5861487fe"
    # The other lane is in scope through the same snapshot and says nothing to continue: skipped for that, not scope.


def test_golden_the_floor_record_lists_the_child_ticket_lane() -> None:
    record = compute_factory_floor(floor_request((issue(ROOT, CHILD),)))
    assert [s.lane for s in record.floor.stopped] == ["labrunpool-1050"]


def test_golden_a_grandchild_is_listed_within_the_depth() -> None:
    linear = (issue(ROOT, "OMN-90001"), issue("OMN-90001", CHILD))
    stopped, _skipped, _cause = build_stopped(floor_request(linear))
    assert [s.lane for s in stopped] == ["labrunpool-1050"]


def test_control_a_ticket_under_no_root_is_out_of_scope() -> None:
    linear = (issue("OMN-90002", CHILD),)
    stopped, _skipped, _cause = build_stopped(floor_request(linear))
    assert stopped == ()
    assert causes(floor_request(linear))["labrunpool-1050"] == "out-of-scope"


def test_control_a_child_in_scope_that_names_nothing_is_not_listed() -> None:
    linear = (issue(ROOT, CHILD, "OMN-23393"),)
    result = build_stopped(floor_request(linear))
    assert [s.lane for s in result[0]] == ["labrunpool-1050"]
    assert causes(floor_request(linear))["p9shell-2250"] == (
        "names-no-branch-ask-or-next-step"
    )


def test_error_chain_no_snapshot_children_reads_every_child_lane_out_of_scope() -> None:
    result = build_stopped(floor_request(()))
    assert result[0] == ()
    assert causes(floor_request(())) == {
        "labrunpool-1050": "out-of-scope",
        "p9shell-2250": "out-of-scope",
    }


def test_error_chain_a_loop_in_the_snapshot_ends_out_of_scope() -> None:
    linear = (issue("OMN-90003", CHILD), issue(CHILD, "OMN-90003"))
    assert build_stopped(floor_request(linear))[0] == ()


def test_the_depth_bound_cuts_a_chain_longer_than_it() -> None:
    linear = (issue(ROOT, "OMN-90001"), issue("OMN-90001", CHILD))
    assert build_stopped(floor_request(linear, depth=2))[0] != ()
    assert build_stopped(floor_request(linear, depth=1))[0] == ()


def test_break_a_scope_that_ignores_children_lists_nothing() -> None:
    """The break the fix removes: with the old exact match on roots, the snapshot's children change nothing."""
    linear = (issue(ROOT, CHILD),)
    stripped = tuple(i.model_copy(update={"children": ()}) for i in linear)
    assert build_stopped(floor_request(linear))[0] != ()
    assert build_stopped(floor_request(stripped))[0] == ()


def test_the_snapshot_loader_keeps_child_ids_and_drops_what_is_not_a_ticket(
    tmp_path: Path,
) -> None:
    path = tmp_path / "linear-2026-11-09.json"
    path.write_text(
        '{"issues": [{"id": "OMN-21288", "status": "In Progress", '
        '"children": ["OMN-23036", "not-a-ticket", 7]}, '
        '{"id": "OMN-23036", "status": "Backlog", "children": null}]}',
        encoding="utf-8",
    )
    issues = {i.id: i for i in load_linear(path)}
    assert issues["OMN-21288"].children == ("OMN-23036",)
    assert issues["OMN-23036"].children == ()


def test_the_title_merge_keeps_the_children_of_the_json_snapshot() -> None:
    from_status_text = (ModelFloorLinearIssue(id=ROOT, status="In Progress"),)
    from_json = (ModelFloorLinearIssue(id=ROOT, title="t", children=(CHILD,)),)
    merged = merge_linear_titles(from_status_text, from_json)
    assert merged[0].children == (CHILD,)
    assert merged[0].title == "t"
