# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The red rule reads NOT WORKING when a red row's owner tickets have no live lane (OMN-23243).

At 2026-11-08T23:42:44Z the served floor had 5 red rows and ``red_with_work_under_way`` 0, yet ``red_rule.state`` read
WORKING with ``unowned_red`` empty: four red rows listed owner tickets and no lane was on any of them, and a ticket id in
``owners`` counted as owned. ``red_rule.uncarried_red`` now names each red row with no live lane and the state follows it.
The declared-owner reading of OMN-22928 (``unowned_red``) is a different reading and stays as it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorCause,
    ModelFloorLane,
    ModelFloorSimple,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    check_red_rule,
)
from tests.nodes.node_floor_simple_compute.test_unowned_red_floor_omn22862 import (
    OWNERS,
    floor_doc,
)

pytestmark = pytest.mark.unit

RECORDED = json.loads(
    (Path(__file__).parent / "fixtures" / "recorded_board_2342.json").read_text(
        encoding="utf-8"
    )
)
CARRIERLESS = (
    "stoppage:lab-idle",
    "stoppage:unclassified",
    "stoppage:quiet-host",
    "piece:1",
)


def recorded_board(
    update: dict[str, dict[str, object]] | None = None,
) -> ModelFloorSimple:
    """A real document from the handler, with the recorded red rows in place of its causes and no piece or lab rows."""
    causes = tuple(
        ModelFloorCause.model_validate({**c, **(update or {}).get(c["id"], {})})
        for c in RECORDED["causes"]
    )
    return floor_doc(OWNERS).model_copy(
        update={"causes": causes, "pieces": (), "lab_state": ()}
    )


def lane(**kw: object) -> ModelFloorLane:
    return ModelFloorLane.model_validate({"lane": "carrier", "liveness": "alive", **kw})


def test_golden_the_recorded_board_reads_not_working_naming_every_red_row_nobody_carries() -> (
    None
):
    doc = recorded_board()
    served = RECORDED["red_rule_as_served"]
    assert (served["state"], served["red_with_work_under_way"]) == ("WORKING", 0)
    reading = check_red_rule(
        doc, tuple(ModelFloorLane.model_validate(x) for x in RECORDED["lanes"])
    )
    assert reading.state == "NOT WORKING"
    assert reading.uncarried_red == CARRIERLESS
    assert reading.violations == ()  # the colour rule itself is as it was
    assert reading.unowned_red == ()  # every one of them lists an owner ticket


def test_golden_a_red_row_with_a_live_lane_on_one_of_its_owner_tickets_is_not_named() -> (
    None
):
    # A red row stays red with a lane on it only when it needs the operator (the colour rule allows that).
    needs = {c: {"needs_operator": True} for c in CARRIERLESS}
    doc = recorded_board(needs)
    on_ticket = lane(lane="on-ticket", ticket="OMN-22478")
    on_parent = lane(lane="on-parent", parent_ticket="OMN-22879")
    on_owns = lane(lane="on-owns", owns=("stoppage:quiet-host",))
    reading = check_red_rule(doc, (on_ticket, on_parent, on_owns))
    assert reading.uncarried_red == ("piece:1",)
    assert reading.state == "NOT WORKING"
    carried = check_red_rule(
        doc, (on_ticket, on_parent, on_owns, lane(lane="p1", ticket="OMN-23119"))
    )
    assert carried.uncarried_red == ()
    assert carried.violations == ()
    assert carried.state == "WORKING"


@pytest.mark.parametrize(
    "ended",
    [
        {"liveness": "dead"},
        {"liveness": "finished"},
        {"stalled": True},
    ],
)
def test_error_a_dead_finished_or_stalled_lane_carries_nothing(
    ended: dict[str, object],
) -> None:
    doc = recorded_board({"stoppage:lab-idle": {"needs_operator": True}})
    gone = lane(lane="gone", ticket="OMN-22866", **ended)
    assert "stoppage:lab-idle" in check_red_rule(doc, (gone,)).uncarried_red


def test_golden_the_declared_owner_reading_is_unchanged() -> None:
    # A red row with an empty owners list is still named in unowned_red, and it has no carrier either.
    doc = recorded_board({"piece:1": {"owners": [], "tickets": []}})
    reading = check_red_rule(doc, ())
    assert reading.unowned_red == ("piece:1",)
    assert "piece:1" in reading.uncarried_red


def test_golden_the_handler_draws_a_row_with_a_lane_amber_and_does_not_name_it() -> (
    None
):
    on = lane(lane="on-it", ticket="OMN-1")
    doc = floor_doc(OWNERS, (on,))
    idle = next(c for c in doc.causes if c.id == "stoppage:lab-idle")
    assert idle.state == "amber"
    assert "stoppage:lab-idle" not in doc.red_rule.uncarried_red
    nobody = floor_doc(OWNERS)
    assert "stoppage:lab-idle" in nobody.red_rule.uncarried_red
    assert nobody.red_rule.state == "NOT WORKING"


def test_golden_the_served_document_and_page_carry_the_reading() -> None:
    doc = recorded_board()
    served = doc.model_copy(update={"red_rule": check_red_rule(doc, ())}).model_dump(
        mode="json"
    )["red_rule"]
    assert served["uncarried_red"] == list(CARRIERLESS)
    page = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/simple.html"
    ).read_text(encoding="utf-8")
    assert "r.uncarried_red" in page
    assert "red with no live lane on it" in page
