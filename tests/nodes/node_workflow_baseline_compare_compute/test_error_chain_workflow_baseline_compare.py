# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: each way a scheduled run fails to show itself running is typed and opens a cause lane (OMN-22959)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_workflow_baseline_compare_compute.handlers import (
    HandlerWorkflowBaselineCompare,
)
from omnimarket.nodes.node_workflow_baseline_compare_compute.models import (
    EnumBaselineVerdict,
    ModelBaselineReading,
    ModelFrozenBaseline,
)
from tests.nodes.node_workflow_baseline_compare_compute.support import (
    COUNT,
    DEADLINE_SECONDS,
    baseline,
    held,
    request,
    result,
)

_HANDLER = HandlerWorkflowBaselineCompare()


def test_error_no_result_past_the_deadline_is_not_working() -> None:
    receipt = _HANDLER.handle(request(None, now_after_s=DEADLINE_SECONDS + 1))

    assert receipt.verdict is EnumBaselineVerdict.NOT_WORKING
    assert receipt.cause_lane is not None
    assert receipt.cause_lane.subjects == ("runtime-result",)


def test_error_a_result_for_another_command_is_not_working_and_uncorrelated() -> None:
    receipt = _HANDLER.handle(request(result(held(), command_id="cmd-0")))

    assert receipt.verdict is EnumBaselineVerdict.NOT_WORKING
    assert receipt.correlated is False
    assert receipt.comparisons == ()
    assert receipt.cause_lane is not None
    assert receipt.cause_lane.subjects == ("correlation",)


def test_error_a_result_observed_before_its_command_is_not_working() -> None:
    receipt = _HANDLER.handle(request(result(held(), observed_after_s=-1)))

    assert receipt.verdict is EnumBaselineVerdict.NOT_WORKING
    assert receipt.correlated is False


def test_error_a_reading_of_the_wrong_kind_is_missing_not_compared() -> None:
    run = result(held())
    wrong = tuple(
        ModelBaselineReading(name=r.name, kind=COUNT, value=r.value)
        if r.name == "route_seconds"
        else r
        for r in run.readings
    )
    receipt = _HANDLER.handle(request(run.model_copy(update={"readings": wrong})))

    assert receipt.verdict is EnumBaselineVerdict.NOT_WORKING
    assert receipt.cause_lane is not None
    assert receipt.cause_lane.subjects == ("route_seconds",)


def test_error_a_baseline_naming_a_metric_twice_is_refused() -> None:
    metric = baseline().metrics[0]

    with pytest.raises(ValidationError):
        ModelFrozenBaseline(
            baseline_id="dup", frozen_at=baseline().frozen_at, metrics=(metric, metric)
        )


def test_error_a_count_that_is_not_whole_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelBaselineReading(name="prs_landed", kind=COUNT, value=1.5)
