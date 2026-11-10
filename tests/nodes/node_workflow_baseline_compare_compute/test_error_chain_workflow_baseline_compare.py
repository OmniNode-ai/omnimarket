# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: each way a scheduled run fails to show itself running is typed and opens a cause lane (OMN-22959)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_workflow_baseline_compare_compute.handlers import (
    HandlerWorkflowBaselineCompare,
)
from omnimarket.nodes.node_workflow_baseline_compare_compute.models import (
    EnumBaselineVerdict,
    EnumBaselineWorseWhen,
    ModelBaselineReading,
    ModelFrozenBaseline,
    ModelFrozenBaselineMetric,
    ModelWorkflowBaselineCompareRequest,
)
from tests.nodes.node_workflow_baseline_compare_compute.support import (
    COUNT,
    DEADLINE_SECONDS,
    ISSUED_AT,
    TIME,
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


def test_error_a_result_naming_a_metric_twice_is_refused() -> None:
    run = result(held())
    nine = ModelBaselineReading(name="prs_landed", kind=COUNT, value=9)
    ten = ModelBaselineReading(name="prs_landed", kind=COUNT, value=10)

    for order in ((nine, ten), (ten, nine)):
        with pytest.raises(ValidationError, match="named twice"):
            run.model_validate(
                {**run.model_dump(), "readings": [*order, *run.readings]}
            )


def test_error_a_result_observed_after_its_deadline_is_not_working() -> None:
    late = _HANDLER.handle(
        request(
            result(held(), observed_after_s=DEADLINE_SECONDS + 1),
            now_after_s=DEADLINE_SECONDS + 2,
        )
    )

    assert late.verdict is EnumBaselineVerdict.NOT_WORKING
    assert late.cause_lane is not None
    assert late.cause_lane.subjects == ("runtime-result",)
    on_time = _HANDLER.handle(
        request(
            result(held(), observed_after_s=DEADLINE_SECONDS),
            now_after_s=DEADLINE_SECONDS + 2,
        )
    )
    assert (
        on_time.verdict is EnumBaselineVerdict.WORKING
    )  # control: compared late, observed on time


def test_error_distinct_run_identities_get_distinct_dispatch_keys() -> None:
    def key(route: str, baseline_id: str) -> str:
        base = baseline().model_copy(update={"baseline_id": baseline_id})
        command = request(None).command.model_copy(
            update={"route": route, "command_id": "cmd-1"}
        )
        receipt = _HANDLER.handle(
            ModelWorkflowBaselineCompareRequest(
                command=command,
                result=None,
                baseline=base,
                now=ISSUED_AT + timedelta(seconds=DEADLINE_SECONDS + 1),
                result_deadline_seconds=DEADLINE_SECONDS,
            )
        )
        assert receipt.cause_lane is not None
        return receipt.cause_lane.dispatch_key

    assert key("daily:west", "v1") != key("daily", "west:v1")
    assert key("daily", "v1") == key("daily", "v1")  # control: stable on redelivery


def test_error_a_tolerance_boundary_is_inclusive_for_decimal_measurements() -> None:
    def metric(
        value: float, tolerance: float, worse_when: EnumBaselineWorseWhen
    ) -> ModelFrozenBaselineMetric:
        return ModelFrozenBaselineMetric(
            name="route_seconds",
            kind=TIME,
            value=value,
            worse_when=worse_when,
            tolerance=tolerance,
        )

    def verdict(m: ModelFrozenBaselineMetric, observed: float) -> EnumBaselineVerdict:
        base = baseline().model_copy(update={"metrics": (m,)})
        run = result({"route_seconds": observed})
        return _HANDLER.handle(
            request(run).model_copy(update={"baseline": base})
        ).verdict

    higher = metric(0.6, 0.3, EnumBaselineWorseWhen.HIGHER)
    lower = metric(0.8, 0.1, EnumBaselineWorseWhen.LOWER)
    assert verdict(higher, 0.9) is EnumBaselineVerdict.WORKING
    assert verdict(lower, 0.7) is EnumBaselineVerdict.WORKING
    # controls: a real regression past the boundary still reads NOT WORKING
    assert verdict(higher, 0.91) is EnumBaselineVerdict.NOT_WORKING
    assert verdict(lower, 0.69) is EnumBaselineVerdict.NOT_WORKING


@pytest.mark.parametrize("bad", [float("inf"), float("nan")])
def test_error_non_finite_measurements_and_tolerances_are_refused(bad: float) -> None:
    with pytest.raises(ValidationError):
        ModelBaselineReading(name="route_seconds", kind=TIME, value=bad)
    with pytest.raises(ValidationError):
        ModelBaselineReading(name="prs_landed", kind=COUNT, value=bad)
    with pytest.raises(ValidationError):
        ModelFrozenBaselineMetric(
            name="route_seconds",
            kind=TIME,
            value=1,
            worse_when=EnumBaselineWorseWhen.HIGHER,
            tolerance=bad,
        )


def test_error_an_overflowing_json_number_is_a_validation_error_not_a_crash() -> None:
    with pytest.raises(ValidationError):
        ModelBaselineReading.model_validate_json(
            '{"name": "prs_landed", "kind": "count", "value": 1e400}'
        )
