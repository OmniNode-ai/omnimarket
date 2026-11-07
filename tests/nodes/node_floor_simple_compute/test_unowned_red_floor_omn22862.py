# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A red cause with no declared owner is named on the board (OMN-22862).

On the night of 2026-11-06 every red row read ``owners: []`` for hours: an owner is declared only in the floor overlay's
``causes.owners`` map and the red cause ids are derived at runtime, so nothing failed while the board said nobody was on
anything. ``red_rule.unowned_red`` now names each red cause that declares no owner, and ``assert_every_red_cause_owned``
is the check a floor test holds a board to. The control is the same board with one owners entry taken away.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorCauseIn,
    ModelFloorLane,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
    check_red_rule,
)
from tests.nodes.node_factory_floor_compute.support import NOW
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

STOPPAGES = tuple(
    ModelFloorCauseIn(
        id=f"stoppage:{name}",
        title=name,
        detail="seen",
        size=3,
        size_unit="occurrences",
    )
    for name in ("lab-idle", "job-exit", "quiet-host")
)
OWNERS = {
    "stoppage:lab-idle": ("OMN-1",),
    "stoppage:job-exit": ("OMN-2",),
    "stoppage:quiet-host": ("OMN-3",),
    "prs:red": ("OMN-4",),
    "piece:17": ("OMN-5",),
}


def floor_doc(
    owners: dict[str, tuple[str, ...]],
    lanes: tuple[ModelFloorLane, ...] = (),
) -> ModelFloorSimple:
    base = overlay_text("node_floor_simple_compute")
    base["causes"] = {
        **base["causes"],
        "owners": {k: list(v) for k, v in owners.items()},
    }
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
            pr_class_counts={"red": 2},
            pr_class_tickets={"red": ("OMN-9",)},
            pr_class_prs={"red": ("r#1", "r#2")},
            stoppages=STOPPAGES,
            lanes=lanes,
            lab=None,
            receipts=None,
            go_given_through=0,
        )
    )


def assert_every_red_cause_owned(doc: ModelFloorSimple) -> None:
    """The check: fails, naming the ids, when a red cause on the board has no owner entry."""
    red = [c.id for c in doc.causes if c.state == "red" and not c.members]
    assert red, "the board has no red cause, so the check read nothing"
    unowned = doc.red_rule.unowned_red
    assert not unowned, f"red causes with no declared owner: {', '.join(unowned)}"


def test_golden_a_board_whose_red_causes_all_declare_an_owner_names_none() -> None:
    doc = floor_doc(OWNERS)
    assert_every_red_cause_owned(doc)
    assert doc.red_rule.unowned_red == ()
    assert {c.id for c in doc.causes if c.state == "red"} >= {
        "stoppage:lab-idle",
        "prs:red",
    }


def test_error_a_red_cause_with_no_owner_entry_is_named() -> None:
    # The failing control: the same board with the owners entry for one stoppage taken away.
    owners = {k: v for k, v in OWNERS.items() if k != "stoppage:quiet-host"}
    doc = floor_doc(owners)
    assert doc.red_rule.unowned_red == ("stoppage:quiet-host",)
    with pytest.raises(AssertionError, match="stoppage:quiet-host"):
        assert_every_red_cause_owned(doc)


def test_error_every_cause_is_named_when_the_owners_map_is_empty() -> None:
    # What the board read on 2026-11-06: no owners map, so every red row reads owners [].
    doc = floor_doc({})
    red = tuple(c.id for c in doc.causes if c.state == "red")
    assert red
    assert doc.red_rule.unowned_red == red
    assert all(c.owners == () for c in doc.causes)


def test_error_a_live_lane_on_a_cause_does_not_make_its_owner_declared() -> None:
    # A lane's own owns= cell turns the cause amber, but the cause still declares no owner entry: when the lane ends
    # it goes red with nobody named, so the entry is what the reading asks for. Amber causes are not red, so not named.
    lane = ModelFloorLane(lane="here", liveness="alive", owns=("stoppage:quiet-host",))
    owners = {k: v for k, v in OWNERS.items() if k != "stoppage:quiet-host"}
    doc = floor_doc(owners, (lane,))
    quiet = next(c for c in doc.causes if c.id == "stoppage:quiet-host")
    assert quiet.state == "amber"
    assert "stoppage:quiet-host" not in doc.red_rule.unowned_red
    gone = floor_doc(owners, (lane.model_copy(update={"liveness": "dead"}),))
    assert "stoppage:quiet-host" in gone.red_rule.unowned_red


def test_golden_the_reading_does_not_change_the_colour_rule() -> None:
    doc = floor_doc({})
    again = check_red_rule(doc, ())
    assert again.state == doc.red_rule.state
    assert again.red_rows == doc.red_rule.red_rows
    assert again.violations == doc.red_rule.violations
