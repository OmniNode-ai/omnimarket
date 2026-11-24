# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: a regression event raises one alarm to the orchestrator lane with a named owner inside 15 minutes and queues one fix lane."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    ModelFixLaneRequest,
    ModelRegressionScanRequest,
    ModelRegressionScanResult,
)
from tests.unit.regression_support import T0, TOPICS, Rig, runs, settings


def _scan(rig: Rig, hours: float) -> ModelRegressionScanResult:
    return rig.handler.handle(
        ModelRegressionScanRequest(now=T0 + timedelta(hours=hours))
    )


def _regression_alarms(rig: Rig) -> list[ModelLabAlarmDeliveryRequest]:
    sent = rig.publisher.on(TOPICS["alarm_delivery_requested"])
    return [
        a
        for a in sent
        if isinstance(a, ModelLabAlarmDeliveryRequest)
        and a.condition == rig.settings.regression_condition
        and a.outcome is EnumDeliveryCondition.ALARM
    ]


def test_one_alarm_row_names_the_check_the_runs_the_owner_and_the_lane() -> None:
    rig = Rig({"C14": runs("gggr"), "C13": runs("gggg")})
    result = _scan(rig, hours=3.1)
    alarms = _regression_alarms(rig)
    assert len(alarms) == 1
    assert len(alarms[0].alarms) == 1
    detail = alarms[0].alarms[0].detail
    assert "C14" in detail
    assert "run-2" in detail
    assert "run-3" in detail
    assert "owner example-owner" in detail
    assert "to example-orchestrator-lane" in detail
    assert result.events[0].late is False


def test_a_scan_inside_fifteen_minutes_of_the_red_run_is_on_time_and_a_late_one_is_marked() -> (
    None
):
    on_time = Rig({"C14": runs("gggr"), "C13": runs("gggg")})
    ok = _scan(on_time, hours=3 + 14 / 60)
    assert ok.events[0].late is False
    assert (
        ok.events[0].alarm_latency_seconds <= on_time.settings.max_alarm_latency_seconds
    )
    late = Rig({"C14": runs("gggr"), "C13": runs("gggg")})
    slow = _scan(late, hours=3 + 16 / 60)
    assert slow.events[0].late is True
    assert (
        slow.events[0].alarm_latency_seconds > late.settings.max_alarm_latency_seconds
    )


def test_the_scan_interval_must_fit_inside_the_alarm_latency_bound() -> None:
    with pytest.raises(ValueError, match="alarm latency"):
        settings(scan_interval_seconds=901)


def test_one_fix_lane_is_queued_for_the_check() -> None:
    rig = Rig({"C14": runs("gggr"), "C13": runs("gggg")})
    result = _scan(rig, hours=3.1)
    assert [f.check_id for f in result.fix_lanes] == ["C14"]
    queued = rig.publisher.on(TOPICS["fix_lane_queued"])
    assert len(queued) == 1
    assert isinstance(queued[0], ModelFixLaneRequest)
    assert queued[0].owner == "example-owner"
    assert queued[0].alarm_lane == "example-orchestrator-lane"


def test_no_second_fix_lane_for_a_check_a_live_lane_holds() -> None:
    rig = Rig({"C14": runs("gggr"), "C13": runs("gggg")}, held={"C14": "fix-c14-lane"})
    result = _scan(rig, hours=3.1)
    assert result.fix_lanes == ()
    assert [(h.check_id, h.held_by) for h in result.held_fixes] == [
        ("C14", "fix-c14-lane")
    ]
    assert rig.publisher.on(TOPICS["fix_lane_queued"]) == []
    assert (
        len(_regression_alarms(rig)) == 1
    )  # the alarm is raised whether or not a lane holds the check


def test_two_transitions_of_one_check_queue_one_fix_lane() -> None:
    rig = Rig({"C14": runs("grgr"), "C13": runs("gggg")})
    result = _scan(rig, hours=3.1)
    assert len(result.events) == 2
    assert len(result.fix_lanes) == 1


def test_a_failed_publish_is_named_and_the_transition_is_retried_on_the_next_scan() -> (
    None
):
    rig = Rig({"C14": runs("gggr"), "C13": runs("gggg")})
    rig.publisher._fail = frozenset({TOPICS["alarm_delivery_requested"]})
    first = _scan(rig, hours=3.1)
    assert first.publish_failures
    rig.publisher._fail = frozenset()
    second = _scan(rig, hours=3.15)
    assert len(second.events) == 1
    assert not second.publish_failures
