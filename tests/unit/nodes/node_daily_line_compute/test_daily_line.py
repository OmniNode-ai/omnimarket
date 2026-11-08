# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily line of rebuild plan section 11 (OMN-22925): its sentence shape, and UNKNOWN for a source not read."""

from __future__ import annotations

import pytest

from omnimarket.models.model_daily_line import (
    ModelDailyLinePiece,
    ModelDailyLineRequest,
)
from omnimarket.nodes.node_daily_line_compute.handlers.handler_daily_line import (
    HandlerDailyLine,
)

pytestmark = pytest.mark.unit

HANDLER = HandlerDailyLine()


def _line(request: ModelDailyLineRequest) -> str:
    return HANDLER.handle(request).line


def test_sentence_shape_is_pinned_for_the_plans_own_example() -> None:
    """The plan's example: piece 1 not yet run, J1 not yet run, N open claims."""
    request = ModelDailyLineRequest(
        piece1=ModelDailyLinePiece(state="UNKNOWN", detail="timer not yet run"),
        j1_state="not yet run",
        open_claims=17,
    )
    assert _line(request) == (
        "Piece 1, code graph: UNKNOWN (timer not yet run), joined to piece 2, hosts, "
        "by J1: not yet run; claims with no TERMINAL: 17."
    )


def test_sentence_shape_working_piece_with_a_passed_join_and_no_open_claims() -> None:
    request = ModelDailyLineRequest(
        piece1=ModelDailyLinePiece(
            state="WORKING", detail="2 nightly runs in 24 hours"
        ),
        j1_state="passed",
        open_claims=0,
    )
    result = HANDLER.handle(request)
    assert result.line == (
        "Piece 1, code graph: WORKING (2 nightly runs in 24 hours), joined to piece 2, hosts, "
        "by J1: passed; claims with no TERMINAL: 0."
    )
    assert result.unknown_parts == ()
    assert [part.name for part in result.parts] == ["piece1", "j1", "claims"]
    assert all(part.read for part in result.parts)


def test_sentence_shape_enum_state_reads_as_the_floor_draws_it() -> None:
    request = ModelDailyLineRequest(
        piece1=ModelDailyLinePiece(
            state="not_working", detail="graph run is 3 days old"
        ),
        j1_state="failed",
        open_claims=4,
    )
    assert _line(request).startswith(
        "Piece 1, code graph: NOT WORKING (graph run is 3 days old),"
    )


def test_sentence_shape_a_state_with_no_detail_has_no_empty_parentheses() -> None:
    request = ModelDailyLineRequest(
        piece1=ModelDailyLinePiece(state="WORKING"), j1_state="passed", open_claims=1
    )
    assert "code graph: WORKING, joined" in _line(request)
    assert "()" not in _line(request)


def test_sentence_shape_a_multiline_source_stays_on_one_line() -> None:
    request = ModelDailyLineRequest(
        piece1=ModelDailyLinePiece(state="WORKING", detail="two\nlines   here"),
        j1_state="passed\n",
        open_claims=1,
    )
    line = _line(request)
    assert "\n" not in line
    assert "(two lines here)" in line
    assert "by J1: passed;" in line


def test_missing_source_every_source_unread_renders_unknown_for_each_part() -> None:
    result = HANDLER.handle(ModelDailyLineRequest())
    assert result.unknown_parts == ("piece1", "j1", "claims")
    assert result.line == (
        "Piece 1, code graph: UNKNOWN (the floor record's piece 1 reading is not read), "
        "joined to piece 2, hosts, by J1: UNKNOWN; claims with no TERMINAL: UNKNOWN."
    )
    assert not any(part.read for part in result.parts)


@pytest.mark.parametrize(
    ("request_kwargs", "unknown"),
    [
        ({"j1_state": "passed", "open_claims": 3}, "piece1"),
        (
            {
                "piece1": ModelDailyLinePiece(state="WORKING"),
                "open_claims": 3,
            },
            "j1",
        ),
        (
            {
                "piece1": ModelDailyLinePiece(state="WORKING"),
                "j1_state": "passed",
            },
            "claims",
        ),
    ],
)
def test_missing_source_one_source_unread_only_its_part_is_unknown(
    request_kwargs: dict[str, object], unknown: str
) -> None:
    result = HANDLER.handle(ModelDailyLineRequest(**request_kwargs))
    assert result.unknown_parts == (unknown,)
    assert [part.name for part in result.parts if part.read] == [
        name for name in ("piece1", "j1", "claims") if name != unknown
    ]
    assert "UNKNOWN" in result.line


def test_missing_source_blank_values_are_unread_and_never_an_empty_cell() -> None:
    request = ModelDailyLineRequest(
        piece1=ModelDailyLinePiece(state="   ", detail=""), j1_state="  ", open_claims=2
    )
    result = HANDLER.handle(request)
    assert result.unknown_parts == ("piece1", "j1")
    assert "by J1: UNKNOWN;" in result.line
    assert "code graph: UNKNOWN (" in result.line
    assert all(part.text.strip() for part in result.parts)


def test_missing_source_a_floor_reading_with_a_blank_state_keeps_the_floors_own_words() -> (
    None
):
    request = ModelDailyLineRequest(
        piece1=ModelDailyLinePiece(state="", detail="timer not yet run"),
        j1_state="not yet run",
        open_claims=0,
    )
    assert _line(request).startswith(
        "Piece 1, code graph: UNKNOWN (timer not yet run),"
    )
