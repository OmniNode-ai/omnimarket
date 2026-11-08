# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-22959): a planted worse count or time publishes NOT WORKING and opens a cause lane."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_workflow_baseline_compare_compute.handlers import (
    HandlerWorkflowBaselineCompare,
)
from omnimarket.nodes.node_workflow_baseline_compare_compute.models import (
    EnumBaselineOutcome,
    EnumBaselineVerdict,
)
from tests.nodes.node_workflow_baseline_compare_compute.support import (
    held,
    request,
    result,
)

_HANDLER = HandlerWorkflowBaselineCompare()


def test_planted_regression_control_the_unplanted_run_reads_working() -> None:
    assert (
        _HANDLER.handle(request(result(held()))).verdict is EnumBaselineVerdict.WORKING
    )


@pytest.mark.parametrize(
    ("planted", "metric"),
    [
        ({"prs_landed": 9}, "prs_landed"),
        ({"red_checks": 3}, "red_checks"),
        ({"route_seconds": 126}, "route_seconds"),
    ],
)
def test_planted_regression_worse_count_or_time_is_not_working_with_a_cause_lane(
    planted: dict[str, float], metric: str
) -> None:
    receipt = _HANDLER.handle(request(result({**held(), **planted})))

    assert receipt.verdict is EnumBaselineVerdict.NOT_WORKING
    worse = [c for c in receipt.comparisons if c.outcome is EnumBaselineOutcome.WORSE]
    assert [c.name for c in worse] == [metric]
    assert receipt.cause_lane is not None
    assert receipt.cause_lane.subjects == (metric,)
    assert receipt.cause_lane.route == "daily-baselines"
    assert receipt.cause_lane.dispatch_key == "daily-baselines:daily-2026-11-01:cmd-1"
    assert metric in receipt.reason


def test_planted_regression_an_unreported_metric_is_not_working() -> None:
    values = held()
    del values["red_checks"]
    receipt = _HANDLER.handle(request(result(values)))

    assert receipt.verdict is EnumBaselineVerdict.NOT_WORKING
    assert receipt.cause_lane is not None
    assert receipt.cause_lane.subjects == ("red_checks",)
    assert [c.outcome for c in receipt.comparisons if c.name == "red_checks"] == [
        EnumBaselineOutcome.MISSING
    ]


def test_planted_regression_a_redelivered_receipt_carries_the_same_dispatch_key() -> (
    None
):
    run = result({**held(), "prs_landed": 1})
    first = _HANDLER.handle(request(run))
    again = _HANDLER.handle(request(run, now_after_s=240))

    assert first.cause_lane is not None
    assert again.cause_lane is not None
    assert first.cause_lane.dispatch_key == again.cause_lane.dispatch_key
