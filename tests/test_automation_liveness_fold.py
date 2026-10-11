# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure automation-liveness fold over the frozen fixture stream (OMN-20802).

Each test names the failure it exists to catch:

* a declared process that never emitted reads as absent instead of as a row
  with its identity, state and digest and nothing else;
* a replayed event changes a row, so a bus redelivery double-counts;
* the fold judges: a process with no verdict event acquires one;
* a delivery or record receipt folded before its raise is lost instead of
  completed by the raise;
* a cleared episode stays open on its process row.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from omnibase_core.enums.enum_liveness_state import EnumLivenessState

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessEvent as Kind,
)
from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessVerdict,
    EnumAutomationProcessState,
    EnumAutomationRunOutcome,
)
from omnimarket.nodes.node_projection_automation_liveness.handlers.handler_projection_automation_liveness import (
    HandlerProjectionAutomationLiveness,
)
from omnimarket.nodes.node_projection_automation_liveness.models import (
    ModelAutomationLivenessFoldRequest,
    ModelAutomationLivenessSnapshot,
)
from tests.helpers.automation_liveness_stream import (
    ACTIVE_HOST,
    ACTIVE_PROCESS,
    SILENT_HOST,
    SILENT_PROCESS,
    STREAM_ORDER,
    fixture_event,
    fixture_stream,
)

pytestmark = pytest.mark.unit

_EPISODE = UUID("6f1d2c3b-4a5e-4f60-8a71-9b8c7d6e5f40")
_FOLD = HandlerProjectionAutomationLiveness()


def _fold_all(
    kinds: tuple[Kind, ...] = STREAM_ORDER,
) -> ModelAutomationLivenessSnapshot:
    held = ModelAutomationLivenessSnapshot()
    for kind in kinds:
        result = _FOLD.handle(
            ModelAutomationLivenessFoldRequest(event=fixture_event(kind), prior=held)
        )
        held = held.merged(result.changes)
    return held


def test_automation_liveness_fold_declared_process_that_never_emitted_is_a_row() -> (
    None
):
    held = _fold_all((Kind.LIVENESS_DECLARED,))
    silent = held.state(SILENT_PROCESS, SILENT_HOST)
    assert silent is not None, "a declared process must be a row before it emits"
    assert silent.declared_at is not None
    assert silent.process_state is EnumAutomationProcessState.ACTIVE
    assert silent.contract_digest is not None
    assert silent.last_run_at is None
    assert silent.last_outcome is None
    assert silent.last_heartbeat_at is None
    assert silent.verdict is None, "the projection does not judge"
    assert silent.failures_in_window == 0
    assert silent.consecutive_idle_with_demand == 0
    assert silent.open_run_started_at is None
    assert silent.open_episode_id is None


def test_automation_liveness_fold_stream_yields_the_expected_row_per_process() -> None:
    held = _fold_all()
    # The two declared processes plus the watchdog the heartbeat names.
    assert {s.key for s in held.states} == {
        (ACTIVE_PROCESS, ACTIVE_HOST),
        (SILENT_PROCESS, SILENT_HOST),
        ("host-c/watchdog-primary", "host-c"),
    }
    active = held.state(ACTIVE_PROCESS, ACTIVE_HOST)
    assert active is not None
    assert active.last_outcome is EnumAutomationRunOutcome.OK
    assert active.last_did_work_count == 0
    assert active.last_demand_count == 4
    assert active.last_work_at is None, "a run that did no work set no work time"
    assert active.consecutive_idle_with_demand == 1
    assert active.failures_in_window == 0
    assert active.open_run_started_at is None
    assert active.verdict is EnumAutomationLivenessVerdict.MISSED
    assert active.verdict_state is EnumLivenessState.STALE
    assert active.open_episode_id is None, "the clear closed the episode"
    silent = held.state(SILENT_PROCESS, SILENT_HOST)
    assert silent is not None
    assert silent.last_run_at is None
    assert silent.verdict is None
    watchdog = held.state("host-c/watchdog-primary", "host-c")
    assert watchdog is not None
    assert watchdog.progress_counter == 131
    assert watchdog.last_heartbeat_at is not None
    assert watchdog.declared_at is None, "the overlay does not declare the watchdog"
    assert len(held.runs) == 1
    assert held.runs[0].run_id == "example-interval-job:2026-10-09T02:10:00Z"


def test_automation_liveness_fold_alarm_episode_keeps_its_receipts() -> None:
    episode = _fold_all().episode(_EPISODE)
    assert episode is not None
    assert episode.process_id == ACTIVE_PROCESS
    assert episode.delivered_at is not None
    assert episode.delivery_ref == "1760002290.000100"
    assert episode.recorded_at is not None
    assert episode.recorded_by == "host-d/watchdog-deadman"
    assert episode.cleared_at is not None


def test_automation_liveness_fold_replay_changes_no_row() -> None:
    held = _fold_all()
    for kind, event in fixture_stream():
        replay = _FOLD.handle(
            ModelAutomationLivenessFoldRequest(event=event, prior=held)
        )
        assert replay.row_count == 0, f"replaying {kind.value} changed a row"


def test_automation_liveness_fold_receipts_before_the_raise_are_completed_by_it() -> (
    None
):
    receipts_first = _fold_all(
        (
            Kind.ALARM_DELIVERED,
            Kind.ALARM_RECORDED,
            Kind.ALARM_CLEARED,
            Kind.ALARM_RAISED,
        )
    )
    in_order = _fold_all(
        (
            Kind.ALARM_RAISED,
            Kind.ALARM_DELIVERED,
            Kind.ALARM_RECORDED,
            Kind.ALARM_CLEARED,
        )
    )
    assert receipts_first.episode(_EPISODE) == in_order.episode(_EPISODE)
    state = receipts_first.state(ACTIVE_PROCESS, ACTIVE_HOST)
    assert state is not None
    assert state.open_episode_id is None, "a cleared episode must not reopen"


def test_automation_liveness_fold_failed_run_counts_and_breaks_the_idle_streak() -> (
    None
):
    base = fixture_event(Kind.RUN_OBSERVED)
    failed = base.model_copy(
        update={
            "run_id": "example-interval-job:2026-10-09T02:15:00Z",
            "started_at": base.started_at.replace(minute=15),
            "finished_at": base.finished_at.replace(minute=15)
            if base.finished_at
            else None,
            "outcome": EnumAutomationRunOutcome.FAILED,
            "exit_code": 1,
        }
    )
    held = _fold_all((Kind.LIVENESS_DECLARED, Kind.RUN_OBSERVED))
    held = held.merged(
        _FOLD.handle(
            ModelAutomationLivenessFoldRequest(event=failed, prior=held)
        ).changes
    )
    state = held.state(ACTIVE_PROCESS, ACTIVE_HOST)
    assert state is not None
    assert state.failures_in_window == 1
    assert state.consecutive_idle_with_demand == 0
    assert state.last_outcome is EnumAutomationRunOutcome.FAILED
