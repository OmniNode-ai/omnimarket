# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: a flap (red then green inside one interval) is counted; the second flap of a check inside 24 hours raises an alarm."""

from __future__ import annotations

from datetime import timedelta

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    ModelRegressionScanRequest,
    ModelRegressionScanResult,
)
from tests.unit.regression_support import T0, TOPICS, Rig, runs


def _scan(rig: Rig, hours: float) -> ModelRegressionScanResult:
    return rig.handler.handle(
        ModelRegressionScanRequest(now=T0 + timedelta(hours=hours))
    )


def _flap_alarms(rig: Rig) -> list[ModelLabAlarmDeliveryRequest]:
    return [
        a
        for a in rig.publisher.on(TOPICS["alarm_delivery_requested"])
        if isinstance(a, ModelLabAlarmDeliveryRequest)
        and a.condition == rig.settings.flap_condition
        and a.outcome is EnumDeliveryCondition.ALARM
    ]


def _count(result: ModelRegressionScanResult, check: str) -> int:
    return next(f.count for f in result.flap_counts if f.check_id == check)


def test_the_first_flap_is_counted_and_raises_no_flap_alarm() -> None:
    rig = Rig({"C14": runs("gggrgg"), "C13": runs("gggggg")})
    result = _scan(rig, hours=5.1)
    assert _count(result, "C14") == 1
    assert _count(result, "C13") == 0
    assert _flap_alarms(rig) == []


def test_the_second_flap_inside_24_hours_raises_a_flap_alarm_naming_the_check() -> None:
    rig = Rig({"C14": runs("gggrggrgg"), "C13": runs("gggggggg")})
    result = _scan(rig, hours=8.1)
    assert _count(result, "C14") == 2
    alarms = _flap_alarms(rig)
    assert len(alarms) == 1
    assert [a.subject for a in alarms[0].alarms] == ["C14"]
    assert "flapped 2 times" in alarms[0].alarms[0].detail
    assert "owner example-owner" in alarms[0].alarms[0].detail


def test_a_red_that_lasts_more_than_one_interval_is_not_a_flap() -> None:
    rig = Rig({"C14": runs("gggrrgg"), "C13": runs("ggggggg")})
    assert _count(_scan(rig, hours=6.1), "C14") == 0


def test_flaps_older_than_the_window_are_not_counted() -> None:
    # two flaps, the first 30 hours before the scan
    rig = Rig({"C14": runs("g" + "grg" + "g" * 30 + "grg"), "C13": runs("g" * 40)})
    result = _scan(rig, hours=37.1)
    assert _count(result, "C14") == 1
    assert _flap_alarms(rig) == []


def test_a_flap_clears_the_episode_when_the_check_stops_flapping() -> None:
    rig = Rig({"C14": runs("gggg"), "C13": runs("gggg")})
    _scan(rig, hours=3.1)
    cleared = [
        a
        for a in rig.publisher.on(TOPICS["alarm_delivery_requested"])
        if isinstance(a, ModelLabAlarmDeliveryRequest)
        and a.condition == rig.settings.flap_condition
        and a.outcome is EnumDeliveryCondition.OK
    ]
    assert len(cleared) == 1
