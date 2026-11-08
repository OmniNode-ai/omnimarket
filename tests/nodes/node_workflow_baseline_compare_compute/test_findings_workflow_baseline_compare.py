# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings on the baseline compare: duplicates, late results, tolerance edge, non-finite values, key collisions (OMN-23190)."""

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
    ModelBaselineCommand,
    ModelBaselineReading,
    ModelBaselineRuntimeResult,
    ModelFrozenBaseline,
    ModelFrozenBaselineMetric,
    ModelWorkflowBaselineCompareRequest,
)
from tests.nodes.node_workflow_baseline_compare_compute.support import (
    DEADLINE_SECONDS,
    ISSUED_AT,
    TIME,
    held,
    request,
    result,
)

_HANDLER = HandlerWorkflowBaselineCompare()
_NON_FINITE = [float("inf"), float("-inf"), float("nan")]


def _reading(name: str, value: float) -> ModelBaselineReading:
    return ModelBaselineReading(name=name, kind=TIME, value=value)


def _boundary_request(
    worse_when: EnumBaselineWorseWhen, value: float, tolerance: float, observed: float
) -> ModelWorkflowBaselineCompareRequest:
    return ModelWorkflowBaselineCompareRequest(
        command=ModelBaselineCommand(
            command_id="cmd-1", route="daily-baselines", issued_at=ISSUED_AT
        ),
        result=ModelBaselineRuntimeResult(
            command_id="cmd-1",
            observed_at=ISSUED_AT + timedelta(seconds=90),
            readings=(_reading("route_seconds", observed),),
        ),
        baseline=ModelFrozenBaseline(
            baseline_id="edge",
            frozen_at=ISSUED_AT - timedelta(days=1),
            metrics=(
                ModelFrozenBaselineMetric(
                    name="route_seconds",
                    kind=TIME,
                    value=value,
                    worse_when=worse_when,
                    tolerance=tolerance,
                ),
            ),
        ),
        now=ISSUED_AT + timedelta(seconds=120),
        result_deadline_seconds=DEADLINE_SECONDS,
    )


def test_duplicate_reading_names_are_refused_whatever_their_order() -> None:
    for readings in (
        (_reading("route_seconds", 1), _reading("route_seconds", 999)),
        (_reading("route_seconds", 999), _reading("route_seconds", 1)),
    ):
        with pytest.raises(ValidationError, match="route_seconds"):
            ModelBaselineRuntimeResult(
                command_id="cmd-1", observed_at=ISSUED_AT, readings=readings
            )


def test_duplicate_free_readings_still_build() -> None:
    run = result(held())

    assert len({r.name for r in run.readings}) == len(run.readings)


def test_a_result_observed_past_the_deadline_is_not_working() -> None:
    late = result(held(), observed_after_s=DEADLINE_SECONDS + 1)

    receipt = _HANDLER.handle(request(late, now_after_s=DEADLINE_SECONDS + 60))

    assert receipt.verdict is EnumBaselineVerdict.NOT_WORKING
    assert receipt.correlated is True
    assert "deadline" in receipt.reason
    assert receipt.cause_lane is not None
    assert "runtime-result" in receipt.cause_lane.subjects


def test_a_result_observed_exactly_at_the_deadline_is_working() -> None:
    on_time = result(held(), observed_after_s=DEADLINE_SECONDS)

    receipt = _HANDLER.handle(request(on_time, now_after_s=DEADLINE_SECONDS + 60))

    assert receipt.verdict is EnumBaselineVerdict.WORKING


def test_boundary_higher_is_worse_holds_exactly_at_value_plus_tolerance() -> None:
    receipt = _HANDLER.handle(
        _boundary_request(EnumBaselineWorseWhen.HIGHER, 0.6, 0.3, 0.9)
    )

    assert receipt.verdict is EnumBaselineVerdict.WORKING


def test_boundary_lower_is_worse_holds_exactly_at_value_minus_tolerance() -> None:
    receipt = _HANDLER.handle(
        _boundary_request(EnumBaselineWorseWhen.LOWER, 0.9, 0.3, 0.6)
    )

    assert receipt.verdict is EnumBaselineVerdict.WORKING


@pytest.mark.parametrize(
    ("worse_when", "observed"),
    [(EnumBaselineWorseWhen.HIGHER, 0.91), (EnumBaselineWorseWhen.LOWER, 0.29)],
)
def test_boundary_one_step_past_the_edge_is_not_working(
    worse_when: EnumBaselineWorseWhen, observed: float
) -> None:
    receipt = _HANDLER.handle(_boundary_request(worse_when, 0.6, 0.3, observed))

    assert receipt.verdict is EnumBaselineVerdict.NOT_WORKING


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_finite_reading_value_refuses_inf_and_nan(bad: float) -> None:
    with pytest.raises(ValidationError):
        _reading("route_seconds", bad)


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_finite_baseline_value_refuses_inf_and_nan(bad: float) -> None:
    with pytest.raises(ValidationError):
        ModelFrozenBaselineMetric(
            name="m",
            kind=TIME,
            value=bad,
            worse_when=EnumBaselineWorseWhen.HIGHER,
            tolerance=0,
        )


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_finite_tolerance_refuses_inf_and_nan(bad: float) -> None:
    with pytest.raises(ValidationError):
        ModelFrozenBaselineMetric(
            name="m",
            kind=TIME,
            value=1,
            worse_when=EnumBaselineWorseWhen.HIGHER,
            tolerance=bad,
        )


@pytest.mark.parametrize("bad", ["Infinity", "NaN"])
def test_finite_json_payload_is_refused(bad: str) -> None:
    payload = '{"name": "m", "kind": "time", "value": ' + bad + "}"
    with pytest.raises(ValidationError):
        ModelBaselineReading.model_validate_json(payload)


def test_dispatch_keys_of_distinct_triples_with_colons_differ() -> None:
    def key(route: str, baseline_id: str, command_id: str) -> str:
        base = request(None, now_after_s=DEADLINE_SECONDS + 1)
        command = base.command.model_copy(
            update={"route": route, "command_id": command_id}
        )
        baseline = base.baseline.model_copy(update={"baseline_id": baseline_id})
        receipt = _HANDLER.handle(
            base.model_copy(update={"command": command, "baseline": baseline})
        )
        assert receipt.cause_lane is not None
        return receipt.cause_lane.dispatch_key

    assert key("a:b", "c", "d") != key("a", "b:c", "d")
    assert key("a", "b:c", "d") != key("a", "b", "c:d")
    assert key("a:b", "c", "d") == key("a:b", "c", "d")
    assert key("daily-baselines", "daily-2026-11-01", "cmd-1") == (
        "daily-baselines:daily-2026-11-01:cmd-1"
    )
