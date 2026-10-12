# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: a single failed run opens a recorded episode at once, and alternating
failed and successful runs raise a posted FAILED (OMN-20806)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAlarmSeverity,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationRunOutcome,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
    ModelRunFact,
)
from tests.nodes.node_automation_liveness_watchdog_compute.support import (
    T0,
    Driver,
    at,
    entry,
    facts,
    overlay,
    raised_for,
    run,
    verdict_of,
)

pytestmark = pytest.mark.unit

_V = EnumAutomationLivenessVerdict
_R = EnumAutomationLivenessReason
_OK = EnumAutomationRunOutcome.OK
_FAILED = EnumAutomationRunOutcome.FAILED


def test_watchdog_failed_run_episodes_single_failure_records_at_once() -> None:
    job = entry("host-a/fast-job", interval=600)
    driver = Driver(overlay(job))
    runs = [run("r1", T0 - timedelta(minutes=10)), run("r2", T0, _FAILED)]

    result = driver.tick(T0 + timedelta(seconds=30), processes=(facts(job, runs),))

    v = verdict_of(result, job.process_id)
    assert (v.verdict, v.reason) == (_V.FAILED, _R.RUN_FAILED)
    [alarm] = result.alarms_raised
    assert alarm.severity is EnumAlarmSeverity.LOW
    assert alarm.opened_at == T0 + timedelta(seconds=30)
    # Recorded now; posted in the next 6-hourly digest plus the delivery budget.
    assert alarm.delivery_due_at == at("2026-10-09T06:02:00")
    assert v.episode_id == alarm.episode_id


def test_watchdog_failed_run_episodes_alternation_posts_high() -> None:
    job = entry("host-a/fast-job", interval=600)
    driver = Driver(overlay(job))
    outcomes = [_FAILED, _OK, _FAILED, _OK]
    runs: list[ModelRunFact] = []
    for i, outcome in enumerate(outcomes):
        finished = T0 + timedelta(minutes=10 * i)
        runs.append(run(f"r{i}", finished, outcome))
        for minute in range(10):
            driver.tick(
                finished + timedelta(minutes=minute, seconds=30),
                processes=(facts(job, runs),),
            )

    alarms = raised_for(driver.raised, job.process_id, _V.FAILED)
    # First failure: low, recorded. It closes after two healthy evaluations.
    # Second failure: two of the last six runs failed, so the window posts high.
    assert [(a.severity, a.reason) for a in alarms] == [
        (EnumAlarmSeverity.LOW, _R.RUN_FAILED),
        (EnumAlarmSeverity.HIGH, _R.FAILURE_WINDOW),
    ]
    high = alarms[1]
    assert high.opened_at == T0 + timedelta(minutes=20, seconds=30)
    assert high.delivery_due_at == high.opened_at + timedelta(seconds=120)
    # After the second success the window still holds: the episode stays open.
    assert verdict_of(driver.results[-1], job.process_id).reason is _R.FAILURE_WINDOW
    assert [c.episode_id for c in driver.cleared] == [alarms[0].episode_id]


def test_watchdog_failed_run_episodes_repeat_failure_raises_severity_once() -> None:
    job = entry("host-a/fast-job", interval=600)
    driver = Driver(overlay(job))
    first = [run("r1", T0, _FAILED)]
    second = [*first, run("r2", T0 + timedelta(minutes=10), _FAILED)]

    driver.tick(T0 + timedelta(seconds=30), processes=(facts(job, first),))
    driver.tick(T0 + timedelta(minutes=1), processes=(facts(job, first),))
    driver.tick(T0 + timedelta(minutes=10, seconds=30), processes=(facts(job, second),))
    driver.tick(T0 + timedelta(minutes=11), processes=(facts(job, second),))

    alarms = raised_for(driver.raised, job.process_id)
    assert [a.severity for a in alarms] == [
        EnumAlarmSeverity.LOW,
        EnumAlarmSeverity.HIGH,
    ]
    # The rise re-raises the same episode; nothing was cleared in between.
    assert alarms[0].episode_id == alarms[1].episode_id
    assert driver.cleared == []


def test_watchdog_failed_run_episodes_slow_cadence_one_failure_is_high() -> None:
    job = entry(
        "host-a/daily-job", interval=86400, max_runtime=3600, deadline=4 * 86400
    )
    driver = Driver(overlay(job))
    failed = [run("d1", T0 - timedelta(days=1)), run("d2", T0, _FAILED)]
    driver.tick(T0 + timedelta(minutes=1), processes=(facts(job, failed),))

    [alarm] = driver.raised
    assert (alarm.severity, alarm.reason) == (EnumAlarmSeverity.HIGH, _R.FAILURE_WINDOW)

    # One healthy run closes a cadence of an hour or slower.
    healed = [*failed, run("d3", T0 + timedelta(hours=2))]
    driver.tick(T0 + timedelta(hours=2, minutes=1), processes=(facts(job, healed),))
    assert [c.episode_id for c in driver.cleared] == [alarm.episode_id]
