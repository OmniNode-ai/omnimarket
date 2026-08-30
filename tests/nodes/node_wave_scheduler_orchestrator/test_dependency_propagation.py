# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""True dependency propagation for node_wave_scheduler_orchestrator [OMN-17017].

RED-first guardrails for the RC-J repair (2026-08-29 beta off-the-rails analysis
rev 2, §RC-J / §4.3 A10 step 3):

* waves are computed against *observed* completion, never against selection;
* a failed dependency removes its dependents from the remaining waves — they are
  never dispatched;
* a dispatcher that returns a partial status map yields a non-COMPLETED result
  and an explicitly UNREPORTED ticket status, never a silent ``"completed"``;
* ``fail_fast`` aborts the run instead of being an inert CLI flag.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_wave_scheduler_orchestrator.handlers.handler_wave_scheduler_orchestrator import (
    HandlerWaveSchedulerOrchestrator,
)
from omnimarket.nodes.node_wave_scheduler_orchestrator.models.model_wave_scheduler_request import (
    ModelWaveSchedulerRequest,
)
from omnimarket.nodes.node_wave_scheduler_orchestrator.models.model_wave_scheduler_result import (
    EnumTicketExecutionStatus,
    EnumWaveSchedulerStatus,
    ModelWaveAssignment,
)


class _RecordingDispatcher:
    """Test dispatcher that records every wave it was asked to dispatch."""

    def __init__(
        self,
        statuses: Mapping[str, EnumTicketExecutionStatus],
        *,
        omit: tuple[str, ...] = (),
    ) -> None:
        self._statuses = dict(statuses)
        self._omit = set(omit)
        self.dispatched_waves: list[tuple[str, ...]] = []

    def dispatch_wave(
        self, assignment: ModelWaveAssignment
    ) -> Mapping[str, EnumTicketExecutionStatus]:
        self.dispatched_waves.append(assignment.ticket_ids)
        return {
            ticket_id: self._statuses[ticket_id]
            for ticket_id in assignment.ticket_ids
            if ticket_id not in self._omit and ticket_id in self._statuses
        }

    @property
    def dispatched_ticket_ids(self) -> set[str]:
        return {ticket for wave in self.dispatched_waves for ticket in wave}


def _write_plan(tmp_path: Path, tickets: list[dict[str, object]]) -> Path:
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.safe_dump({"tickets": tickets}), encoding="utf-8")
    return plan


@pytest.mark.unit
def test_failed_wave_one_does_not_dispatch_wave_two(tmp_path: Path) -> None:
    """Two-wave synthetic epic: wave 1 fails, wave 2 is NEVER dispatched."""
    plan = _write_plan(
        tmp_path,
        [
            {"ticket_id": "OMN-1", "repo": "omnimarket"},
            {"ticket_id": "OMN-2", "repo": "omnibase_core", "depends_on": ["OMN-1"]},
        ],
    )
    dispatcher = _RecordingDispatcher(
        {
            "OMN-1": EnumTicketExecutionStatus.FAILED,
            "OMN-2": EnumTicketExecutionStatus.COMPLETED,
        }
    )
    handler = HandlerWaveSchedulerOrchestrator(dispatcher=dispatcher)

    result = handler.handle(
        ModelWaveSchedulerRequest(
            plan_path=str(plan), state_dir=str(tmp_path / "state")
        )
    )

    assert dispatcher.dispatched_ticket_ids == {"OMN-1"}
    assert "OMN-2" not in dispatcher.dispatched_ticket_ids
    assert result.run_status is EnumWaveSchedulerStatus.PARTIAL
    assert result.tickets_failed == 1
    assert result.tickets_blocked == 1
    blocked = {
        ticket_id: status
        for summary in result.wave_execution_summaries
        for ticket_id, status in summary.ticket_statuses
    }
    assert blocked["OMN-2"] is EnumTicketExecutionStatus.BLOCKED


@pytest.mark.unit
def test_failure_cascade_is_transitive(tmp_path: Path) -> None:
    """A failure blocks the whole transitive dependent closure, not just wave 2."""
    plan = _write_plan(
        tmp_path,
        [
            {"ticket_id": "OMN-1"},
            {"ticket_id": "OMN-2", "depends_on": ["OMN-1"]},
            {"ticket_id": "OMN-3", "depends_on": ["OMN-2"]},
            {"ticket_id": "OMN-9"},
        ],
    )
    dispatcher = _RecordingDispatcher(
        {
            "OMN-1": EnumTicketExecutionStatus.FAILED,
            "OMN-2": EnumTicketExecutionStatus.COMPLETED,
            "OMN-3": EnumTicketExecutionStatus.COMPLETED,
            "OMN-9": EnumTicketExecutionStatus.COMPLETED,
        }
    )
    handler = HandlerWaveSchedulerOrchestrator(dispatcher=dispatcher)

    result = handler.handle(
        ModelWaveSchedulerRequest(
            plan_path=str(plan), state_dir=str(tmp_path / "state")
        )
    )

    assert dispatcher.dispatched_ticket_ids == {"OMN-1", "OMN-9"}
    assert result.tickets_blocked == 2
    assert result.tickets_completed == 1
    assert result.run_status is EnumWaveSchedulerStatus.PARTIAL


@pytest.mark.unit
def test_partial_status_map_is_unreported_not_completed(tmp_path: Path) -> None:
    """A dispatcher that omits a ticket must NEVER produce a COMPLETED result."""
    plan = _write_plan(tmp_path, [{"ticket_id": "OMN-1"}, {"ticket_id": "OMN-2"}])
    dispatcher = _RecordingDispatcher(
        {
            "OMN-1": EnumTicketExecutionStatus.COMPLETED,
            "OMN-2": EnumTicketExecutionStatus.COMPLETED,
        },
        omit=("OMN-2",),
    )
    handler = HandlerWaveSchedulerOrchestrator(dispatcher=dispatcher)

    result = handler.handle(
        ModelWaveSchedulerRequest(
            plan_path=str(plan), state_dir=str(tmp_path / "state")
        )
    )

    assert result.run_status is not EnumWaveSchedulerStatus.COMPLETED
    assert result.tickets_unreported == 1
    statuses = {
        ticket_id: status
        for summary in result.wave_execution_summaries
        for ticket_id, status in summary.ticket_statuses
    }
    assert statuses["OMN-2"] is EnumTicketExecutionStatus.UNREPORTED


@pytest.mark.unit
def test_unreported_dependency_blocks_dependents(tmp_path: Path) -> None:
    """An unreported ticket is not a completion — its dependents stay undispatched."""
    plan = _write_plan(
        tmp_path,
        [{"ticket_id": "OMN-1"}, {"ticket_id": "OMN-2", "depends_on": ["OMN-1"]}],
    )
    dispatcher = _RecordingDispatcher(
        {"OMN-2": EnumTicketExecutionStatus.COMPLETED}, omit=("OMN-1",)
    )
    handler = HandlerWaveSchedulerOrchestrator(dispatcher=dispatcher)

    result = handler.handle(
        ModelWaveSchedulerRequest(
            plan_path=str(plan), state_dir=str(tmp_path / "state")
        )
    )

    assert dispatcher.dispatched_ticket_ids == {"OMN-1"}
    assert result.tickets_blocked == 1


@pytest.mark.unit
def test_dispatcher_status_outside_the_enum_is_a_hard_error(tmp_path: Path) -> None:
    """An unknown status string fails fast rather than being coerced."""

    class _BadDispatcher:
        def dispatch_wave(
            self, assignment: ModelWaveAssignment
        ) -> Mapping[str, EnumTicketExecutionStatus]:
            return {assignment.ticket_ids[0]: "not-a-status"}  # type: ignore[dict-item]

    plan = _write_plan(tmp_path, [{"ticket_id": "OMN-1"}])
    handler = HandlerWaveSchedulerOrchestrator(dispatcher=_BadDispatcher())

    with pytest.raises(ValueError, match="not-a-status"):
        handler.handle(
            ModelWaveSchedulerRequest(
                plan_path=str(plan), state_dir=str(tmp_path / "state")
            )
        )


@pytest.mark.unit
def test_fail_fast_aborts_remaining_waves(tmp_path: Path) -> None:
    """``--fail-fast`` is a live flag: the run aborts on the first wave failure."""
    plan = _write_plan(
        tmp_path,
        [
            {"ticket_id": "OMN-1"},
            {"ticket_id": "OMN-9"},
            {"ticket_id": "OMN-2", "depends_on": ["OMN-9"]},
        ],
    )
    dispatcher = _RecordingDispatcher(
        {
            "OMN-1": EnumTicketExecutionStatus.FAILED,
            "OMN-9": EnumTicketExecutionStatus.COMPLETED,
            "OMN-2": EnumTicketExecutionStatus.COMPLETED,
        }
    )
    handler = HandlerWaveSchedulerOrchestrator(dispatcher=dispatcher)

    result = handler.handle(
        ModelWaveSchedulerRequest(
            plan_path=str(plan),
            fail_fast=True,
            state_dir=str(tmp_path / "state"),
        )
    )

    assert result.run_status is EnumWaveSchedulerStatus.ABORTED
    assert "OMN-2" not in dispatcher.dispatched_ticket_ids
    # Wave ids stay unique: the undispatched summary is keyed off the number of
    # dispatched waves, not the loop counter the fail_fast break left behind.
    assert [s.wave_id for s in result.wave_execution_summaries] == [0, 1]
    statuses = {
        ticket_id: status
        for summary in result.wave_execution_summaries
        for ticket_id, status in summary.ticket_statuses
    }
    assert statuses["OMN-2"] is EnumTicketExecutionStatus.SKIPPED


@pytest.mark.unit
def test_all_green_run_is_completed(tmp_path: Path) -> None:
    """The happy path still reports COMPLETED and dispatches every wave in order."""
    plan = _write_plan(
        tmp_path,
        [
            {"ticket_id": "OMN-1"},
            {"ticket_id": "OMN-2", "depends_on": ["OMN-1"]},
        ],
    )
    dispatcher = _RecordingDispatcher(
        {
            "OMN-1": EnumTicketExecutionStatus.COMPLETED,
            "OMN-2": EnumTicketExecutionStatus.COMPLETED,
        }
    )
    handler = HandlerWaveSchedulerOrchestrator(dispatcher=dispatcher)

    result = handler.handle(
        ModelWaveSchedulerRequest(
            plan_path=str(plan), state_dir=str(tmp_path / "state")
        )
    )

    assert dispatcher.dispatched_waves == [("OMN-1",), ("OMN-2",)]
    assert result.run_status is EnumWaveSchedulerStatus.COMPLETED
    assert result.tickets_completed == 2
    assert result.tickets_unreported == 0


@pytest.mark.unit
def test_resumed_is_only_true_when_state_was_actually_read(tmp_path: Path) -> None:
    """``resumed`` reports a real checkpoint read, never an unconditional echo."""
    plan = _write_plan(tmp_path, [{"ticket_id": "OMN-1"}])
    dispatcher = _RecordingDispatcher({"OMN-1": EnumTicketExecutionStatus.COMPLETED})
    handler = HandlerWaveSchedulerOrchestrator(dispatcher=dispatcher)

    first = handler.handle(
        ModelWaveSchedulerRequest(
            plan_path=str(plan), resume=True, state_dir=str(tmp_path / "state")
        )
    )

    assert first.resumed is False
    assert dispatcher.dispatched_ticket_ids == {"OMN-1"}

    resumed_dispatcher = _RecordingDispatcher(
        {"OMN-1": EnumTicketExecutionStatus.COMPLETED}
    )
    resumed_handler = HandlerWaveSchedulerOrchestrator(dispatcher=resumed_dispatcher)
    second = resumed_handler.handle(
        ModelWaveSchedulerRequest(
            plan_path=str(plan), resume=True, state_dir=str(tmp_path / "state")
        )
    )

    assert second.resumed is True
    assert resumed_dispatcher.dispatched_ticket_ids == set()
    assert second.run_status is EnumWaveSchedulerStatus.COMPLETED


@pytest.mark.unit
def test_live_run_without_injected_dispatcher_does_not_raise(tmp_path: Path) -> None:
    """dry_run=false no longer raises unconditionally — the default effect
    boundary (durable state-store dispatch lifecycle) is real."""
    plan = _write_plan(tmp_path, [{"ticket_id": "OMN-1"}])
    handler = HandlerWaveSchedulerOrchestrator()

    result = handler.handle(
        ModelWaveSchedulerRequest(
            plan_path=str(plan), state_dir=str(tmp_path / "state")
        )
    )

    assert result.run_status is not EnumWaveSchedulerStatus.COMPLETED
    assert result.tickets_unreported == 1
    assert result.dispatch_lifecycle_path is not None
    assert Path(result.dispatch_lifecycle_path).is_file()
