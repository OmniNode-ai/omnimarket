# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rebuild piece is admitted when its start criterion reads true, not only on a contiguous go (RULING 2026-11-07T12:20:46Z).

The floor admitted a piece when the go covered it from piece 1 with no gap, so piece 9 (whose go was a ruling of another
ticket, with pieces 7 and 8 never granted) could never be admitted. The overlay's ``rebuild.start_after`` now names, per
piece, the pieces whose reading must be WORKING; a piece with no entry is admitted only by the go or as a step 0 piece.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
    ModelReadingRow,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 23, 0, tzinfo=UTC)
ADMITTED = "admitted; no golden and error event yet"


def _overlay(start_after: dict[int, tuple[int, ...]]) -> ModelFloorSimpleOverlay:
    base = ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )
    return base.model_copy(
        update={"rebuild": base.rebuild.model_copy(update={"start_after": start_after})}
    )


def _reading(piece: int, state: str) -> ModelReadingRow:
    return ModelReadingRow(
        piece=piece,
        state=state,  # type: ignore[arg-type]
        probed_at=NOW - timedelta(minutes=5),
        published_at=NOW - timedelta(minutes=4),
        max_age_s=7200,
    )


def _rows(
    start_after: dict[int, tuple[int, ...]],
    readings: dict[int, str],
    *,
    go: int | None = 0,
) -> dict[int, str]:
    floor = HandlerFloorSimple().handle(
        ModelFloorSimpleInput(
            now=NOW,
            overlay=_overlay(start_after),
            open_prs=10,
            open_prs_before=12,
            merges_in_window=4,
            watcher_read_at=NOW - timedelta(minutes=1),
            slots_working=5,
            slots_total=20,
            record_read_at=NOW - timedelta(minutes=1),
            pr_class_counts={},
            pr_class_tickets={},
            pr_class_prs={},
            lanes=(),
            lab=None,
            receipts=None,
            go_given_through=go,
            readings={p: _reading(p, s) for p, s in readings.items()},
        )
    )
    return {p.piece: p.reason for p in floor.pieces}


def test_golden_a_piece_whose_predecessor_reads_working_is_admitted_with_no_go() -> (
    None
):
    reasons = _rows({9: (8,)}, {8: "WORKING"}, go=0)
    assert ADMITTED in reasons[9]


def test_error_the_same_piece_without_a_criterion_is_not_started() -> None:
    reasons = _rows({}, {8: "WORKING"}, go=0)
    assert reasons[9] == "not started"


def test_error_a_predecessor_that_is_not_working_or_unread_does_not_admit() -> None:
    assert _rows({9: (8,)}, {8: "NOT WORKING"}, go=0)[9] == "not started"
    assert _rows({9: (8,)}, {8: "UNKNOWN"}, go=0)[9] == "not started"
    assert _rows({9: (8,)}, {}, go=0)[9] == "not started"


def test_error_every_named_predecessor_must_read_working() -> None:
    both = _rows({9: (7, 8)}, {7: "NOT WORKING", 8: "WORKING"}, go=0)
    assert both[9] == "not started"
    assert ADMITTED in _rows({9: (7, 8)}, {7: "WORKING", 8: "WORKING"}, go=0)[9]


def test_error_a_piece_with_no_entry_is_not_admitted_by_its_predecessor() -> None:
    reasons = _rows({9: (8,)}, {6: "WORKING"}, go=0)
    assert reasons[7] == "not started"


def test_golden_the_go_still_admits_a_piece_the_criterion_does_not() -> None:
    reasons = _rows({}, {}, go=3)
    assert ADMITTED in reasons[3]
