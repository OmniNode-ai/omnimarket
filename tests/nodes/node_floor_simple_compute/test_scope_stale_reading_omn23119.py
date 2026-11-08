# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A piece outside the readiness overlay's in_scope_pieces never draws a red row (OMN-23119, AC2).

At 2026-11-08T07:16Z the writer's 06:15Z tick had skipped pieces 6 and 7, so both readings were 7,257 s old against a
7,200 s bound and both read NOT WORKING. Piece 6 is in scope. Piece 7 is not, yet it drew a red row with an ``unowned``
cause of its own. The controls below fail on that board and pass on the rule:

- a stale reading of a piece outside the scope draws no red row and opens no cause;
- the same stale reading of a piece inside the scope still draws red (the failing control);
- widening the scope to take in the piece brings the red row back (the injected break);
- the scope is overlay data, never a list in the handler.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelReadingRow,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from tests.nodes.node_floor_simple_compute.test_readiness_omn23081 import (
    NOW,
    SCOPE,
    overlay,
    piece,
    receipts,
)

pytestmark = pytest.mark.unit

BOUND = 7200
STALE_FOR = 7257  # the age the served floor read for pieces 6 and 7 at 07:16Z


def board(
    *, scope: tuple[int, ...] = SCOPE, stale: tuple[int, ...] = (6, 7)
) -> ModelFloorSimple:
    """Every piece reads fresh and WORKING except the ``stale`` ones, whose reading is STALE_FOR seconds old."""
    ov = overlay(in_scope_pieces=scope)
    readings = {
        p: ModelReadingRow(
            piece=p,
            state="WORKING",
            probed_at=NOW - timedelta(seconds=STALE_FOR if p in stale else 300),
            published_at=NOW - timedelta(seconds=STALE_FOR if p in stale else 300),
            max_age_s=BOUND,
        )
        for p in (1, 2, 3, 4, 5, 6, 7, 8, 9, 17)
    }
    return HandlerFloorSimple().handle(
        ModelFloorSimpleInput(
            now=NOW,
            overlay=ov,
            open_prs=0,
            open_prs_before=0,
            merges_in_window=0,
            watcher_read_at=NOW,
            slots_working=1,
            slots_total=2,
            record_read_at=NOW,
            lanes=(),
            lab=None,
            receipts=receipts(ov),
            readings=readings,
            go_given_through=18,
        )
    )


def test_error_a_stale_piece_outside_the_scope_draws_no_red_row() -> None:
    assert 7 not in SCOPE
    doc = board()
    row = piece(doc, 7)
    assert row.reading_age_s == STALE_FOR
    assert row.reading_max_age_s == BOUND
    assert row.draw != "red"
    assert row.in_scope is False
    assert all(c.id != "piece:7" for c in doc.causes)
    assert "piece:7" not in doc.red_rule.unowned_red


def test_golden_a_stale_piece_inside_the_scope_still_draws_red() -> None:
    """The failing control: the same age on piece 6, which is in scope, is red with its own cause."""
    doc = board()
    row = piece(doc, 6)
    assert row.status == "not_working"
    assert row.draw == "red"
    assert any(c.id == "piece:6" for c in doc.causes)


def test_error_widening_the_scope_to_piece_7_brings_its_red_row_back() -> None:
    """The injected break: with piece 7 in the overlay's scope, the same reading is red."""
    doc = board(scope=(*SCOPE, 7))
    assert piece(doc, 7).draw == "red"
    assert any(c.id == "piece:7" for c in doc.causes)


def test_golden_every_in_scope_piece_inside_its_bound_draws_no_red_row() -> None:
    doc = board(stale=())
    assert [p.piece for p in doc.pieces if p.draw == "red"] == []
