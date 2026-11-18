# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24174: Chart 3 and the headline read one named context arm and replicate of the newest run."""

from __future__ import annotations

import itertools

import pytest

from omnimarket.models.eval_item.model_eval_snapshot import (
    ModelEvalSnapshotRun,
)
from omnimarket.nodes.node_eval_board_compute.handlers import HandlerEvalBoard
from omnimarket.nodes.node_eval_board_compute.handlers.board_chart1 import latest_run
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

_OFF = run("r2", FIRST_TEST + WEEK, context_arm="off", rows=rates((89, 85), (89, 80)))
_ON = run(
    "r2",
    FIRST_TEST + WEEK + WEEK,
    context_arm="on",
    rows=rates((89, 60), (89, 50)),
)
_OFF_SECOND_REPLICATE = run(
    "r2",
    FIRST_TEST + WEEK + 2 * WEEK,
    context_arm="off",
    replicate=1,
    rows=rates((89, 70), (89, 60)),
)
_EARLIER = run("r1", FIRST_TEST, context_arm="on", rows=rates((89, 40), (89, 40)))


def _chart3_small_passed(*runs: ModelEvalSnapshotRun) -> int:
    record = HandlerEvalBoard().handle(request(model(LOCAL_ID, tuple(runs))))
    rate = record.chart3.rows[0].cells[0].rate
    assert rate is not None
    return rate.passed


@pytest.mark.parametrize(
    "order",
    list(itertools.permutations((_EARLIER, _OFF, _ON, _OFF_SECOND_REPLICATE))),
)
def test_eval_board_a_snapshot_of_two_arms_reads_the_arm_that_sorts_first(
    order: tuple[ModelEvalSnapshotRun, ...],
) -> None:
    chosen = latest_run(model(LOCAL_ID, order))
    assert chosen is not None
    assert (chosen.run_id, chosen.context_arm, chosen.replicate) == ("r2", "off", 0)
    # The newest by test time is the second replicate of arm "off"; the choice is the first.
    assert _chart3_small_passed(*order) == 85


def test_eval_board_the_headline_and_chart3_read_the_same_arm() -> None:
    record = HandlerEvalBoard().handle(request(model(LOCAL_ID, (_EARLIER, _ON, _OFF))))
    cell = record.chart3.rows[0].cells[0].rate
    assert cell is not None
    # Arm "off": small 85 of 89 and medium 80 of 89, so 165 of 178 overall; arm "on" reads 110 of 178.
    assert cell.passed == 85
    assert "passed 165 of 178 tasks" in record.headline
    assert record.chart3.rows[0].tested_at == _OFF.last_scored_at


def test_eval_board_a_model_with_no_closed_run_has_no_latest_run() -> None:
    assert latest_run(model(LOCAL_ID)) is None
