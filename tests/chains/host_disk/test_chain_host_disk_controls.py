# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC6 (OMN-23866): failing control per part. An injected break turns its golden test red.

Each case first runs the golden assertion on the unbroken chain (the positive control: it passes), then injects the
break and shows the same assertion fail. Compute break: the floor is ignored, so no under-floor alarm is decided.
Orchestrator break: a result is never collected, so the run is never recorded with its results.
"""

from __future__ import annotations

import pytest

from omnimarket.models.model_host_disk_decision import (
    EnumHostDiskAlarmKind,
    ModelHostDiskDecision,
    ModelHostDiskDecisionRequest,
)
from omnimarket.models.model_host_disk_orchestrator import (
    EnumHostDiskRunOutcome,
    ModelHostDiskCommandOutcome,
)
from omnimarket.nodes.node_host_disk_decision_compute.handlers.handler_host_disk_decision import (
    decide_host_disk,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers import run_core, run_state
from tests.nodes.node_host_disk_decision_compute.decision_inputs import GIB
from tests.nodes.node_host_disk_orchestrator.orchestrator_inputs import (
    HOSTS,
    SLACK_TOPIC,
    Orchestrator,
    policy,
)

from .host_disk_chain import InMemoryWire, new_key, run_chain

pytestmark = pytest.mark.unit


async def _golden_alarm(wire: InMemoryWire) -> None:
    """The golden assertion for the compute: a host under its floor raises its alarm on the wire."""
    key = new_key()
    await run_chain(
        wire,
        Orchestrator(pol=policy(floor_free_bytes=900 * GIB)),
        key,
        stale_revisions=0,
    )
    alarms = await wire.read(SLACK_TOPIC, key, len(HOSTS))
    assert len(alarms) == len(HOSTS), "a host under its floor must raise its alarm"


async def _golden_collected(wire: InMemoryWire) -> None:
    """The golden assertion for the orchestrator: every result is collected into the recorded run."""
    key = new_key()
    records = await run_chain(wire, Orchestrator(), key)
    assert [r.outcome for r in records] == [EnumHostDiskRunOutcome.COMPLETED]
    assert [r.status for r in records[0].results] == ["completed"] * len(HOSTS)


def _floor_ignored(request: ModelHostDiskDecisionRequest) -> ModelHostDiskDecision:
    honest = decide_host_disk(request)
    return honest.model_copy(
        update={
            "alarms": tuple(
                a
                for a in honest.alarms
                if a.kind is not EnumHostDiskAlarmKind.UNDER_FLOOR
            )
        }
    )


async def test_compute_control_golden_passes_then_goes_red_when_the_floor_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _golden_alarm(InMemoryWire())  # positive control
    monkeypatch.setattr(run_core, "decide_host_disk", _floor_ignored)
    with pytest.raises(AssertionError, match="must raise its alarm"):
        await _golden_alarm(InMemoryWire())


async def test_orchestrator_control_golden_passes_then_goes_red_when_a_result_is_not_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _golden_collected(InMemoryWire())  # positive control

    def not_collected(
        self: run_state.RunBook, outcome: ModelHostDiskCommandOutcome
    ) -> None:
        return None

    monkeypatch.setattr(run_state.RunBook, "record", not_collected)
    with pytest.raises(AssertionError):
        await _golden_collected(InMemoryWire())
