# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A red row reads "owner unknown" with the read's age when the owner map or the lane list cannot be trusted (OMN-23388).

From 16:07Z to past 22:00Z on 2026-11-09 every red row read "nobody on it" while 10 to 30 lanes ran, because the owner
map came from an overlay store whose last read was hours old. The floor now judges the two inputs of "who is on it": the
owner map by the reading of the overlay store source the overlay names, the lane list by the record's read time. When
either is older than its declared bound or was not read, a row with no lane on it reads ``owner_state: unknown`` with
the age, and is named in ``red_rule.owner_unknown_red``, never in ``unowned_red``. With both inputs fresh and read, a row
with no lane still reads unowned: the control that keeps the new reading from muting the old one.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorCauseIn,
    ModelFloorLane,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
    ModelLabStateOutput,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
    check_red_rule,
)
from tests.nodes.node_factory_floor_compute.support import NOW
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

STORE = "overlay-store"
OWNER_MAP_MAX_AGE_S = 900
LANES_MAX_AGE_S = 600
CAUSES = tuple(
    ModelFloorCauseIn(
        id=f"stoppage:{name}",
        title=name,
        detail="seen",
        size=3,
        size_unit="occurrences",
    )
    for name in ("lab-idle", "job-exit")
)
OWNERS = {"stoppage:lab-idle": ["OMN-1"], "stoppage:job-exit": ["OMN-2"]}
LANE = ModelFloorLane(lane="here", ticket="OMN-1", liveness="alive")


def store_verdict(age_s: int | None, state: str = "WORKING") -> dict[str, Any]:
    return {
        "id": STORE,
        "title": "Overlay store",
        "kind": "overlay_store",
        "state": state,
        "probed_at": None if age_s is None else NOW - timedelta(seconds=age_s),
        "published_at": NOW,
        "age_s": age_s,
        "max_age_s": OWNER_MAP_MAX_AGE_S,
        "reason": "" if state == "WORKING" else "judged",
        "summary": "",
        "subjects": (),
        "tickets": (),
    }


def doc(
    *,
    store_age_s: int | None = 60,
    store_state: str = "WORKING",
    record_age_s: int | None = 60,
    lanes: tuple[ModelFloorLane, ...] = (),
    judged: bool = True,
    lab_present: bool = True,
) -> ModelFloorSimple:
    base = overlay_text("node_floor_simple_compute")
    base["causes"] = {**base["causes"], "owners": OWNERS}
    if judged:
        base["causes"]["owner_inputs"] = {
            "owner_map_source": STORE,
            "lanes_max_age_s": LANES_MAX_AGE_S,
        }
    lab = (
        ModelLabStateOutput.model_validate(
            {
                "computed_at": NOW,
                "verdicts": (store_verdict(store_age_s, store_state),),
            }
        )
        if lab_present
        else None
    )
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
            record_read_at=None
            if record_age_s is None
            else NOW - timedelta(seconds=record_age_s),
            stoppages=CAUSES,
            lanes=lanes,
            lab=lab,
            receipts=None,
            go_given_through=0,
        )
    )


def cause(d: ModelFloorSimple, cause_id: str) -> Any:
    return next(c for c in d.causes if c.id == cause_id)


def test_golden_fresh_complete_inputs_a_row_with_no_lane_still_reads_unowned() -> None:
    d = doc()
    row = cause(d, "stoppage:job-exit")
    assert row.state == "red"
    assert row.work_under_way is False
    assert row.owner_state == "unowned"
    assert d.owner_inputs is not None
    assert d.owner_inputs.unknown is False
    assert d.red_rule.owner_unknown_red == ()
    assert "stoppage:job-exit" in d.red_rule.uncarried_red


def test_golden_fresh_inputs_a_row_with_a_lane_reads_owned() -> None:
    d = doc(lanes=(LANE,))
    assert cause(d, "stoppage:lab-idle").owner_state == "owned"
    assert cause(d, "stoppage:job-exit").owner_state == "unowned"


def test_error_a_stale_owner_map_reads_owner_unknown_with_the_age() -> None:
    d = doc(store_age_s=319 * 60, store_state="NOT WORKING")
    row = cause(d, "stoppage:job-exit")
    assert row.work_under_way is False
    assert row.owner_state == "unknown"
    assert "owner map" in row.owner_note
    assert "319 min" in row.owner_note
    assert d.owner_inputs is not None
    assert d.owner_inputs.unknown
    assert d.owner_inputs.owner_map_age_s == 319 * 60
    # The stale store is itself a lab cause (drawn through lab:state); the two stoppages are named beside it.
    assert set(d.red_rule.owner_unknown_red) >= {
        "stoppage:lab-idle",
        "stoppage:job-exit",
    }
    assert d.red_rule.unowned_red == ()
    assert d.red_rule.uncarried_red == ()
    assert all(c.owner_state != "unowned" for c in d.causes)


def test_error_an_unreadable_owner_map_reads_owner_unknown() -> None:
    d = doc(store_age_s=None, store_state="UNKNOWN")
    row = cause(d, "stoppage:lab-idle")
    assert row.owner_state == "unknown"
    assert "unreadable" in row.owner_note
    assert d.owner_inputs is not None
    assert d.owner_inputs.owner_map_age_s is None


def test_error_an_owner_map_source_the_lab_did_not_report_reads_owner_unknown() -> None:
    d = doc(lab_present=False)
    assert cause(d, "stoppage:lab-idle").owner_state == "unknown"


def test_error_a_stale_lane_list_reads_owner_unknown_with_the_age() -> None:
    d = doc(record_age_s=3 * 3600)
    row = cause(d, "stoppage:job-exit")
    assert row.owner_state == "unknown"
    assert "lane list" in row.owner_note
    assert "180 min" in row.owner_note
    assert d.owner_inputs is not None
    assert d.owner_inputs.lanes_age_s == 3 * 3600


def test_error_an_unreadable_lane_list_reads_owner_unknown() -> None:
    d = doc(record_age_s=None)
    assert cause(d, "stoppage:lab-idle").owner_state == "unknown"
    assert d.owner_inputs is not None
    assert d.owner_inputs.lanes_age_s is None


def test_golden_a_stale_map_never_hides_a_lane_that_is_on_the_row() -> None:
    d = doc(store_age_s=319 * 60, store_state="NOT WORKING", lanes=(LANE,))
    assert cause(d, "stoppage:lab-idle").owner_state == "owned"
    assert cause(d, "stoppage:job-exit").owner_state == "unknown"


def test_golden_the_colour_is_not_muted_by_an_unknown_owner() -> None:
    fresh = doc()
    stale = doc(store_age_s=319 * 60, store_state="NOT WORKING")

    def stoppages(d: ModelFloorSimple) -> list[tuple[str, str]]:
        return [(c.id, c.state) for c in d.causes if c.id.startswith("stoppage:")]

    assert stoppages(stale) == stoppages(fresh)
    assert all(state == "red" for _, state in stoppages(stale))
    assert stale.red_rule.state != "WORKING"


def test_golden_the_reading_recomputes_from_the_document() -> None:
    d = doc(store_age_s=319 * 60, store_state="NOT WORKING")
    again = check_red_rule(d, ())
    assert again.owner_unknown_red == d.red_rule.owner_unknown_red
    assert again.state == d.red_rule.state


def test_golden_an_overlay_that_declares_no_owner_inputs_judges_none() -> None:
    d = doc(judged=False, store_age_s=319 * 60, store_state="NOT WORKING")
    assert d.owner_inputs is None
    assert all(c.owner_state != "unknown" for c in d.causes)
    assert d.red_rule.owner_unknown_red == ()


def test_error_the_page_draws_owner_unknown_not_nobody_on_it() -> None:
    from pathlib import Path

    page = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/simple.html"
    ).read_text(encoding="utf-8")
    assert 'c.owner_state === "unknown"' in page
    assert "Owner unknown" in page
