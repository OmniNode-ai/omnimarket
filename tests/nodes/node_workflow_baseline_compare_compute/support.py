# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the baseline compare tests (OMN-22959): fixtures only, no node logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_workflow_baseline_compare_compute.models import (
    EnumBaselineMetricKind,
    EnumBaselineWorseWhen,
    ModelBaselineCommand,
    ModelBaselineReading,
    ModelBaselineRuntimeResult,
    ModelFrozenBaseline,
    ModelFrozenBaselineMetric,
    ModelWorkflowBaselineCompareRequest,
)

NAME = "node_workflow_baseline_compare_compute"
ISSUED_AT = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
DEADLINE_SECONDS = 600

COUNT = EnumBaselineMetricKind.COUNT
TIME = EnumBaselineMetricKind.TIME


def baseline() -> ModelFrozenBaseline:
    """A baseline written time-first on purpose, so the receipt's order is the node's, not the input's."""
    return ModelFrozenBaseline(
        baseline_id="daily-2026-11-01",
        frozen_at=ISSUED_AT - timedelta(days=7),
        metrics=(
            ModelFrozenBaselineMetric(
                name="route_seconds",
                kind=TIME,
                value=120,
                worse_when=EnumBaselineWorseWhen.HIGHER,
                tolerance=5,
            ),
            ModelFrozenBaselineMetric(
                name="prs_landed",
                kind=COUNT,
                value=10,
                worse_when=EnumBaselineWorseWhen.LOWER,
                tolerance=0,
            ),
            ModelFrozenBaselineMetric(
                name="red_checks",
                kind=COUNT,
                value=2,
                worse_when=EnumBaselineWorseWhen.HIGHER,
                tolerance=0,
            ),
        ),
    )


def held() -> dict[str, float]:
    return {"prs_landed": 10, "red_checks": 2, "route_seconds": 120}


def result(
    values: dict[str, float],
    command_id: str = "cmd-1",
    observed_after_s: int = 90,
) -> ModelBaselineRuntimeResult:
    kinds = {m.name: m.kind for m in baseline().metrics}
    return ModelBaselineRuntimeResult(
        command_id=command_id,
        observed_at=ISSUED_AT + timedelta(seconds=observed_after_s),
        readings=tuple(
            ModelBaselineReading(name=n, kind=kinds[n], value=v)
            for n, v in values.items()
        ),
    )


def request(
    run: ModelBaselineRuntimeResult | None,
    now_after_s: int = 120,
) -> ModelWorkflowBaselineCompareRequest:
    return ModelWorkflowBaselineCompareRequest(
        command=ModelBaselineCommand(
            command_id="cmd-1", route="daily-baselines", issued_at=ISSUED_AT
        ),
        result=run,
        baseline=baseline(),
        now=ISSUED_AT + timedelta(seconds=now_after_s),
        result_deadline_seconds=DEADLINE_SECONDS,
    )
