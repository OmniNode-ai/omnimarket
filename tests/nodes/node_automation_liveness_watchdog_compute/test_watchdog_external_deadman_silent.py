# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC8: no completed external dead-man run for 2 hours raises
EXTERNAL_DEADMAN_SILENT (OMN-20806)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAlarmSeverity,
    EnumAutomationEmitter,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationRunOutcome,
    EnumAutomationTriggerKind,
    ModelAutomationLivenessEntry,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
    ModelAutomationLivenessWatchdogResult,
)
from tests.nodes.node_automation_liveness_watchdog_compute.support import (
    T0,
    Driver,
    entry,
    facts,
    overlay,
    run,
    verdict_of,
)

pytestmark = pytest.mark.unit

_V = EnumAutomationLivenessVerdict
_DEADMAN = "github/external-deadman"


def _deadman() -> ModelAutomationLivenessEntry:
    # A scheduled workflow every 15 minutes, read through the GitHub observer.
    return entry(
        _DEADMAN,
        host="github",
        kind=EnumAutomationTriggerKind.GH_SCHEDULE,
        interval=900,
        max_runtime=600,
        max_silence_seconds=3 * 3600,
        deadline=4 * 3600,
        demand="none",
        emitter=EnumAutomationEmitter.OBSERVER,
    )


def _tick_minutes(
    driver: Driver,
    deadman: ModelAutomationLivenessEntry,
    last_completed: datetime,
    start: datetime,
    count: int,
) -> list[ModelAutomationLivenessWatchdogResult]:
    results = []
    for minute in range(count):
        now = start + timedelta(minutes=minute)
        results.append(
            driver.tick(
                now,
                processes=(facts(deadman, [run("ext", last_completed, demand=None)]),),
                external_deadman_process_id=_DEADMAN,
            )
        )
    return results


def test_watchdog_external_deadman_silent_after_two_hours() -> None:
    deadman = _deadman()
    driver = Driver(overlay(deadman))
    last = T0
    # 1 h 58 min to 2 h 3 min after the last completed run.
    results = _tick_minutes(driver, deadman, last, T0 + timedelta(minutes=118), 6)

    before = verdict_of(results[1], _DEADMAN)  # 1 h 59 min
    assert before.verdict is _V.HEALTHY
    at_two = verdict_of(results[2], _DEADMAN)  # 2 h 0 min
    assert (at_two.verdict, at_two.reason) == (
        _V.EXTERNAL_DEADMAN_SILENT,
        EnumAutomationLivenessReason.EXTERNAL_RUN_MISSING,
    )
    [alarm] = driver.raised
    assert alarm.verdict is _V.EXTERNAL_DEADMAN_SILENT
    assert alarm.severity is EnumAlarmSeverity.HIGH
    assert alarm.opened_at == T0 + timedelta(hours=2, minutes=1)


def test_watchdog_external_deadman_silent_failed_runs_do_not_count_as_absent() -> None:
    deadman = _deadman()
    driver = Driver(overlay(deadman))
    now = T0 + timedelta(minutes=30)
    failed = facts(
        deadman,
        [
            run(
                "ext",
                T0 + timedelta(minutes=29),
                EnumAutomationRunOutcome.FAILED,
                demand=None,
            )
        ],
    )
    result = driver.tick(now, processes=(failed,), external_deadman_process_id=_DEADMAN)
    # A completed run that failed is FAILED, not silent.
    assert verdict_of(result, _DEADMAN).verdict is _V.FAILED


def test_watchdog_external_deadman_silent_never_ran_ages_from_declaration() -> None:
    deadman = _deadman()
    driver = Driver(overlay(deadman))
    declared = facts(deadman, [], declared_at=T0)
    early = driver.tick(
        T0 + timedelta(minutes=90),
        processes=(declared,),
        external_deadman_process_id=_DEADMAN,
    )
    assert verdict_of(early, _DEADMAN).verdict is _V.HEALTHY
    late = driver.tick(
        T0 + timedelta(hours=2),
        processes=(declared,),
        external_deadman_process_id=_DEADMAN,
    )
    assert verdict_of(late, _DEADMAN).verdict is _V.EXTERNAL_DEADMAN_SILENT


def test_watchdog_external_deadman_silent_only_for_the_named_process() -> None:
    deadman = _deadman()
    driver = Driver(overlay(deadman))
    stale = facts(deadman, [run("ext", T0, demand=None)])
    result = driver.tick(T0 + timedelta(hours=2, minutes=30), processes=(stale,))
    # Not named as the external dead-man: judged by its own max_silence (3 h).
    assert verdict_of(result, _DEADMAN).verdict is _V.HEALTHY
