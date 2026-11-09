# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A job-exit cell does not say a lane was dispatched when the host cannot dispatch one (OMN-22881).

The recorded case: the floor row ``stoppage:job-exit`` read four episodes on 2026-11-08, each written as
``lane job-exit-fix dispatched for episode ...`` while the same tick's STATUS row read ``dispatched=no``
(``dispatch unconfigured``). A reader of the FRICTION row took a lane to be on each episode, and the row
stayed red with no owner. The cell now says no lane was sent when dispatch is unconfigured.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_silent_failure_alarm_compute.handlers.handler_job_exit_alarm import (
    HandlerJobExitAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.models import (
    ModelJobExitAlarmRequest,
    ModelJobExitAlarmResult,
    ModelJobExitReading,
    ModelJobExitStreak,
)

LABEL = "ai.omninode.work-ledger-copy"
NOW = datetime(2026, 11, 8, 15, 8, tzinfo=UTC)


def _tick(
    *,
    dispatch_configured: bool,
    runs: int = 11,
    last_exit: int = 1,
    finding_exits: dict[str, tuple[int, ...]] | None = None,
) -> ModelJobExitAlarmResult:
    previous = (
        ModelJobExitStreak(
            label=LABEL, runs=10, last_exit=0, last_run_at=NOW - timedelta(minutes=5)
        ),
    )
    return HandlerJobExitAlarm().handle(
        ModelJobExitAlarmRequest(
            now=NOW,
            readable=True,
            readings=(
                ModelJobExitReading(label=LABEL, runs=runs, last_exit=last_exit),
            ),
            previous=previous,
            threshold=1,
            finding_exits=finding_exits or {},
            dispatch_configured=dispatch_configured,
        )
    )


def test_an_unsent_lane_is_not_called_dispatched_for_a_failed_exit() -> None:
    cell = _tick(dispatch_configured=False).friction_cells[0]
    assert "cause=failed-exit" in cell
    assert "dispatched" not in cell
    assert "no lane sent for episode ep-20261108T1508Z-work-ledger-copy" in cell
    assert "dispatch unconfigured" in cell


def test_an_unsent_lane_is_not_called_dispatched_for_a_finding_exit() -> None:
    cell = _tick(dispatch_configured=False, finding_exits={LABEL: (1,)}).friction_cells[
        0
    ]
    assert "cause=finding-exit" in cell
    assert "dispatched" not in cell
    assert "no lane sent for episode" in cell


def test_an_unsent_lane_is_not_called_dispatched_for_a_missed_interval() -> None:
    stale = ModelJobExitStreak(
        label=LABEL, runs=10, last_exit=0, last_run_at=NOW - timedelta(hours=5)
    )
    result = HandlerJobExitAlarm().handle(
        ModelJobExitAlarmRequest(
            now=NOW,
            readable=True,
            readings=(ModelJobExitReading(label=LABEL, runs=10, last_exit=0),),
            previous=(stale,),
            threshold=1,
            intervals={LABEL: 3600},
            dispatch_configured=False,
        )
    )
    cell = result.friction_cells[0]
    assert "cause=missed-interval" in cell
    assert "dispatched" not in cell
    assert "no lane sent for episode" in cell


@pytest.mark.parametrize("finding", [False, True])
def test_a_configured_dispatch_still_says_the_lane_was_dispatched(
    finding: bool,
) -> None:
    result = _tick(
        dispatch_configured=True, finding_exits={LABEL: (1,)} if finding else None
    )
    cell = result.friction_cells[0]
    assert "lane job-exit-fix dispatched for episode ep-20261108T1508Z-" in cell
    assert "no lane sent" not in cell
