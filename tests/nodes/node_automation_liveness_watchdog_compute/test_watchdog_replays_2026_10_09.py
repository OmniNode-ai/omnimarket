# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1, verdict half: each of the 2026-10-09 silent failures, replayed, raises
its verdict with a delivery due time inside its deadline (OMN-20806).

Delivery itself (the Slack timestamp, the outbox) belongs to the effect node.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAlarmSeverity,
    EnumAutomationLivenessVerdict,
)
from tests.nodes.node_automation_liveness_watchdog_compute.replay_2026_10_09 import (
    CASES,
    LANE_RUNNER,
    ReplayCase,
    replay,
)
from tests.nodes.node_automation_liveness_watchdog_compute.support import Driver

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def replayed() -> Driver:
    return replay()


@pytest.mark.parametrize(
    "case", CASES, ids=lambda c: f"{c.process_id}@{c.fault_start:%H%M}"
)
def test_watchdog_replays_2026_10_09(replayed: Driver, case: ReplayCase) -> None:
    entry = next(
        e for e in replayed.overlay.processes if e.process_id == case.process_id
    )
    deadline = case.fault_start + timedelta(seconds=entry.alarm_deadline_seconds)
    posted = [
        r
        for r in replayed.raised
        if r.process_id == case.process_id
        and r.verdict is case.verdict
        and r.severity is EnumAlarmSeverity.HIGH
        and r.opened_at >= case.fault_start
    ]
    assert posted, f"{case.evidence}: no {case.verdict.value} raised"
    first = posted[0]
    assert first.reason is case.reason
    assert first.delivery_due_at <= deadline, (
        f"{case.evidence}: due {first.delivery_due_at} after deadline {deadline}"
    )


def test_watchdog_replays_2026_10_09_raises_nothing_else(replayed: Driver) -> None:
    expected = {(c.process_id, c.verdict) for c in CASES}
    raised = {(r.process_id, r.verdict) for r in replayed.raised}
    assert raised == expected
    # The refusals were recorded low at the first refused run, then posted.
    runner = [r for r in replayed.raised if r.process_id == LANE_RUNNER]
    assert [r.severity for r in runner] == [
        EnumAlarmSeverity.LOW,
        EnumAlarmSeverity.HIGH,
    ]
    assert len({r.episode_id for r in runner}) == 1
    assert not any(r.void for r in replayed.results)
    assert not any(
        r.verdict is EnumAutomationLivenessVerdict.HOST_SILENT for r in replayed.raised
    )


def test_watchdog_replays_2026_10_09_recoveries_close_their_episodes(
    replayed: Driver,
) -> None:
    open_at_end = {
        p.process_id: p.episode.verdict
        for p in replayed.state.processes
        if p.episode is not None
    }
    # Only the unreadable slot filler is still failing when the replay ends.
    assert open_at_end == {
        "host-a/slot-filler": EnumAutomationLivenessVerdict.UNOBSERVABLE
    }
    assert len(replayed.cleared) == len({r.episode_id for r in replayed.raised}) - 1
