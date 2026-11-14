# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24174 AC1: every point is one run-rate row, and a size is named only when it and every smaller size clear."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.nodes.node_eval_board_compute.handlers import HandlerEvalBoard
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_record import (
    ModelEvalBoardSizeRow,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRunState,
)
from tests.nodes.node_eval_board_compute.support import (
    FIRST_TEST,
    LOCAL_ID,
    WEEK,
    model,
    rates,
    request,
    run,
)

pytestmark = pytest.mark.unit

CLEARS = (100, 100)
FAILS = (100, 70)


def _sizes(
    small: tuple[int, int],
    medium: tuple[int, int],
    large: tuple[int, int] | None,
) -> ModelEvalBoardSizeRow:
    record = HandlerEvalBoard().handle(
        request(
            model(LOCAL_ID, (run("r1", FIRST_TEST, rows=rates(small, medium, large)),))
        )
    )
    return record.chart3.rows[0]


def test_two_rows_for_one_run_date_are_two_points() -> None:
    same_day = FIRST_TEST
    record = HandlerEvalBoard().handle(
        request(
            model(
                LOCAL_ID,
                (
                    run("r1", same_day, rows=rates((89, 60), (89, 62))),
                    run("r2", same_day, rows=rates((89, 61), (89, 63))),
                ),
            )
        )
    )
    (series,) = record.chart1.series
    assert [(p.rate.passed, p.rate.n) for p in series.points] == [
        (122, 178),
        (124, 178),
    ]
    assert series.points[0].tested_at == series.points[1].tested_at


def test_a_void_run_and_a_run_without_an_overall_row_draw_no_point() -> None:
    record = HandlerEvalBoard().handle(
        request(
            model(
                LOCAL_ID,
                (
                    run("r1", FIRST_TEST),
                    run(
                        "r2",
                        FIRST_TEST + WEEK,
                        state=EnumEvalRunState.VOID,
                        void_reason="registration_mismatch",
                    ),
                    run("r3", FIRST_TEST + 2 * WEEK, rows=rates()[1:]),
                ),
            )
        )
    )
    (series,) = record.chart1.series
    assert len(series.points) == 1
    assert record.footer.void_notes == (
        "Local coding model (Qwen)'s test on 2026-11-09 was not counted: registration mismatch.",
    )


def test_medium_clearing_while_small_does_not_names_no_size() -> None:
    row = _sizes(FAILS, CLEARS, CLEARS)
    assert row.reliable_up_to is None
    assert row.reliable_text == "no size is reliable yet"
    assert [c.reliable for c in row.cells] == [False, True, True]


def test_small_and_large_clearing_with_medium_failing_reads_reliable_up_to_small() -> (
    None
):
    row = _sizes(CLEARS, FAILS, CLEARS)
    assert row.reliable_up_to == "small"
    assert row.reliable_text == "reliable up to small: small tasks cleared 90%"


def test_every_size_clearing_names_the_largest() -> None:
    row = _sizes(CLEARS, CLEARS, CLEARS)
    assert row.reliable_up_to == "large"
    assert row.reliable_text == (
        "reliable up to large: small, medium and large tasks all cleared 90%"
    )


def test_small_and_medium_clearing_reads_both_cleared() -> None:
    row = _sizes(CLEARS, CLEARS, None)
    assert (
        row.reliable_text
        == "reliable up to medium: small and medium tasks both cleared 90%"
    )
    assert row.cells[2].rate is None


def test_the_cells_come_from_the_latest_closed_run() -> None:
    record = HandlerEvalBoard().handle(
        request(
            model(
                LOCAL_ID,
                (
                    run("r1", FIRST_TEST, rows=rates(CLEARS, CLEARS, CLEARS)),
                    run("r2", FIRST_TEST + WEEK, rows=rates(FAILS, FAILS, FAILS)),
                ),
            )
        )
    )
    assert record.chart3.rows[0].reliable_up_to is None
    assert record.chart3.rows[0].tested_at == FIRST_TEST + timedelta(days=7)
