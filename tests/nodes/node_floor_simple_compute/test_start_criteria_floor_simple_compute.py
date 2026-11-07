# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A piece starts when its own start criterion reads true, not on a go (OMN-22864, RULING 2026-11-07T12:20:46Z).

Golden: a planted piece whose prerequisites read WORKING is admitted with no go covering it. Error: the same piece with a
prerequisite that does not read WORKING stays not started. The named break ``criteria-dropped`` removes the criterion and
turns the golden red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
    ModelReadingRow,
    ModelStartCriterion,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 23, 0, tzinfo=UTC)
PLANTED = 9
PREREQUISITE = 8


def _reading(piece: int, state: str) -> ModelReadingRow:
    return ModelReadingRow(
        piece=piece,
        state=state,  # type: ignore[arg-type]
        probed_at=NOW - timedelta(minutes=5),
        published_at=NOW - timedelta(minutes=4),
        max_age_s=86400,
    )


def _input(
    prerequisite_state: str, *, criteria: bool = True, go: int | None = 6
) -> ModelFloorSimpleInput:
    base = ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )
    rebuild = base.rebuild.model_copy(
        update={
            "start_criteria": (
                (ModelStartCriterion(piece=PLANTED, after_working=(PREREQUISITE,)),)
                if criteria
                else ()
            )
        }
    )
    return ModelFloorSimpleInput(
        now=NOW,
        overlay=base.model_copy(update={"rebuild": rebuild}),
        open_prs=1,
        open_prs_before=1,
        merges_in_window=1,
        watcher_read_at=NOW - timedelta(minutes=1),
        slots_working=1,
        slots_total=2,
        record_read_at=NOW - timedelta(minutes=1),
        pr_class_counts={},
        pr_class_tickets={},
        pr_class_prs={},
        lanes=(),
        lab=None,
        receipts=None,
        go_given_through=go,
        readings={PREREQUISITE: _reading(PREREQUISITE, prerequisite_state)},
    )


def _status(doc, piece: int) -> str:
    return next(p.status for p in doc.pieces if p.piece == piece)


def test_golden_a_piece_with_a_true_start_criterion_and_no_go_is_admitted() -> None:
    doc = HandlerFloorSimple().handle(_input("WORKING"))
    row = next(p for p in doc.pieces if p.piece == PLANTED)
    assert PLANTED > 6  # no go covers it
    assert row.status == "unknown"
    assert "admitted" in row.text


def test_error_a_prerequisite_that_does_not_read_working_leaves_the_piece_not_started() -> (
    None
):
    for state in ("NOT WORKING", "UNKNOWN"):
        assert _status(HandlerFloorSimple().handle(_input(state)), PLANTED) == (
            "not_started"
        )


def test_error_the_named_break_criteria_dropped_turns_the_golden_red() -> None:
    doc = HandlerFloorSimple().handle(_input("WORKING", criteria=False))
    assert _status(doc, PLANTED) == "not_started"


def test_error_a_criterion_that_names_itself_or_a_piece_out_of_range_is_refused() -> (
    None
):
    with pytest.raises(ValueError, match="other pieces between 1 and 18"):
        ModelStartCriterion(piece=9, after_working=(9,))
    with pytest.raises(ValueError, match="other pieces between 1 and 18"):
        ModelStartCriterion(piece=9, after_working=(19,))
