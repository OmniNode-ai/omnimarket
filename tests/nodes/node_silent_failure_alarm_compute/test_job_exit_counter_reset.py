# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A launchd run counter that restarts lower after a unit reload is a new run (OMN-22881).

The recorded case, read from the host's alarm state on 2026-11-07: the alarm held runs 1018 for
the consumer-group guard, 337 for lab-fill and 10 for the prrp snapshot publisher, while launchd
now read 232, 67 and 2, because each unit had been unloaded and loaded again and launchd counts
from 1 after a load. The alarm advanced only on ``reading.runs > stored`` so it saw no run for
those jobs for hours, kept a missed-interval episode open on a job that was running every
interval, and floor row stoppage:job-exit stayed red on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_silent_failure_alarm_compute.handlers.handler_job_exit_alarm import (
    HandlerJobExitAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.models import (
    ModelJobExitAlarmRequest,
    ModelJobExitAlarmResult,
    ModelJobExitReading,
    ModelJobExitStreak,
)

LABEL = "ai.omninode.consumer-group-guard"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
INTERVAL_S = 300


def _tick(
    runs: int | None,
    last_exit: int | None,
    previous: tuple[ModelJobExitStreak, ...],
    now: datetime,
) -> ModelJobExitAlarmResult:
    return HandlerJobExitAlarm().handle(
        ModelJobExitAlarmRequest(
            now=now,
            readable=True,
            readings=(
                ModelJobExitReading(
                    label=LABEL, runs=runs, last_exit=last_exit, interval_s=INTERVAL_S
                ),
            ),
            previous=previous,
            threshold=1,
            push_after_minutes=30,
        )
    )


def _stale_open_episode() -> tuple[ModelJobExitStreak, ...]:
    """The state on the host: counter 1018, last run seen 22 hours ago, episode open."""
    return (
        ModelJobExitStreak(
            label=LABEL,
            runs=1018,
            last_exit=0,
            last_run_at=NOW - timedelta(hours=22),
            episode="ep-20261106T1414Z-consumer-group-guard-missed",
            opened_at=NOW - timedelta(hours=21),
            cause="missed-interval",
        ),
    )


def test_a_counter_that_restarted_lower_is_a_new_run_and_closes_the_missed_episode() -> (
    None
):
    result = _tick(232, 0, _stale_open_episode(), NOW)
    assert result.closed == ("ep-20261106T1414Z-consumer-group-guard-missed",)
    assert result.opened == ()
    kept = {item.label: item for item in result.streaks}[LABEL]
    assert kept.runs == 232
    assert kept.episode is None
    assert kept.last_run_at == NOW


def test_a_running_job_after_a_reload_does_not_open_a_missed_episode() -> None:
    baseline = (
        ModelJobExitStreak(
            label=LABEL, runs=1018, last_exit=0, last_run_at=NOW - timedelta(hours=22)
        ),
    )
    result = _tick(232, 0, baseline, NOW)
    assert result.opened == ()
    assert result.state == "OK"


def test_a_reload_that_has_not_run_yet_is_not_a_run() -> None:
    result = _tick(0, None, _stale_open_episode(), NOW)
    kept = {item.label: item for item in result.streaks}[LABEL]
    assert kept.last_run_at == NOW - timedelta(hours=22)
    assert kept.episode == "ep-20261106T1414Z-consumer-group-guard-missed"


def test_the_same_restarted_counter_on_the_next_tick_is_not_another_run() -> None:
    first = _tick(232, 0, _stale_open_episode(), NOW)
    later = _tick(232, 0, first.streaks, NOW + timedelta(minutes=20))
    kept = {item.label: item for item in later.streaks}[LABEL]
    assert kept.last_run_at == NOW
    assert len(later.opened) == 1
    assert later.opened[0].cause == "missed-interval"


def test_a_failed_run_after_a_reload_still_counts_toward_the_streak() -> None:
    baseline = (
        ModelJobExitStreak(
            label=LABEL, runs=1018, last_exit=0, last_run_at=NOW - timedelta(minutes=4)
        ),
    )
    result = _tick(1, 1, baseline, NOW)
    assert result.state == "FAILING"
    assert result.opened[0].cause == "failed-exit"
