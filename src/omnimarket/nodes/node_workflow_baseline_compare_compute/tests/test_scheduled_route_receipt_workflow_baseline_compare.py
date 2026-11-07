# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-22959): a scheduled run shows the command time, the correlated result and a comparison, counts before times."""

from __future__ import annotations

from datetime import timedelta

from omnimarket.nodes.node_workflow_baseline_compare_compute.handlers import (
    HandlerWorkflowBaselineCompare,
)
from omnimarket.nodes.node_workflow_baseline_compare_compute.models import (
    EnumBaselineMetricKind,
    EnumBaselineOutcome,
    EnumBaselineVerdict,
)
from tests.nodes.node_workflow_baseline_compare_compute.support import (
    ISSUED_AT,
    held,
    request,
    result,
)

_HANDLER = HandlerWorkflowBaselineCompare()


def test_scheduled_route_receipt_shows_command_time_correlated_result_and_comparison() -> (
    None
):
    run = result(held())
    receipt = _HANDLER.handle(request(run))

    assert receipt.verdict is EnumBaselineVerdict.WORKING
    assert receipt.cause_lane is None
    assert receipt.route == "daily-baselines"
    assert receipt.command_id == "cmd-1"
    assert receipt.command_issued_at == ISSUED_AT
    assert receipt.result_command_id == "cmd-1"
    assert receipt.result_observed_at == ISSUED_AT + timedelta(seconds=90)
    assert receipt.correlated is True
    assert receipt.baseline_id == "daily-2026-11-01"
    assert {c.outcome for c in receipt.comparisons} == {EnumBaselineOutcome.OK}
    assert {c.name: c.observed_value for c in receipt.comparisons} == held()


def test_scheduled_route_receipt_compares_counts_before_times() -> None:
    receipt = _HANDLER.handle(request(result(held())))

    assert [c.kind for c in receipt.comparisons] == [
        EnumBaselineMetricKind.COUNT,
        EnumBaselineMetricKind.COUNT,
        EnumBaselineMetricKind.TIME,
    ]
    assert [c.name for c in receipt.comparisons] == [
        "prs_landed",
        "red_checks",
        "route_seconds",
    ]


def test_scheduled_route_receipt_within_tolerance_still_reads_working() -> None:
    receipt = _HANDLER.handle(request(result({**held(), "route_seconds": 125})))

    assert receipt.verdict is EnumBaselineVerdict.WORKING


def test_scheduled_route_receipt_result_not_due_yet_reads_unknown_without_a_cause_lane() -> (
    None
):
    receipt = _HANDLER.handle(request(None, now_after_s=300))

    assert receipt.verdict is EnumBaselineVerdict.UNKNOWN
    assert receipt.correlated is False
    assert receipt.result_command_id is None
    assert receipt.comparisons == ()
    assert receipt.cause_lane is None
