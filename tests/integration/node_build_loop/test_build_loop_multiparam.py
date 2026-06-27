# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_build_loop.

WS-5 Wave 8 (OMN-13682). Variant A — the COMPUTE FSM handler is driven
in-process via ``handle(command, phase_results)``. The build loop is a pure,
deterministic state machine (no I/O), so each case varies the execution mode and
the per-phase success map and asserts the typed terminal state + completed-event
payload (final phase, cycle counts, error message).

Negative control: a case feeds a failing phase result; the FSM must NOT reach
COMPLETE — it stalls in-phase with a recorded ``error_message`` and zero
completed cycles. A run that reported COMPLETE for a failing phase would be a
regression.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.nodes.node_build_loop.handlers.handler_build_loop import (
    HandlerBuildLoop,
)
from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
    ModelLoopStartCommand,
)
from omnimarket.nodes.node_build_loop.models.model_loop_state import (
    EnumBuildLoopPhase,
)


def _command(mode: str, *, skip_closeout: bool = False) -> ModelLoopStartCommand:
    return ModelLoopStartCommand(
        correlation_id=uuid4(),
        mode=mode,
        skip_closeout=skip_closeout,
        requested_at=datetime.now(tz=UTC),
    )


# Each success case: (mode, skip_closeout, expected_min_transitions, forbid_phase)
_SUCCESS_CASES = [
    pytest.param("build", False, 5, None, id="build-mode-full-sequence"),
    pytest.param("observe", False, 1, None, id="observe-mode-verify-only"),
    pytest.param("close_out", False, 5, None, id="close-out-mode-full-sequence"),
    pytest.param(
        "build",
        True,
        4,
        EnumBuildLoopPhase.CLOSING_OUT,
        id="build-mode-skip-closeout",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("mode", "skip_closeout", "expected_min_transitions", "forbid_phase"),
    _SUCCESS_CASES,
)
def test_build_loop_success_paths(
    mode: str,
    skip_closeout: bool,
    expected_min_transitions: int,
    forbid_phase: EnumBuildLoopPhase | None,
) -> None:
    command = _command(mode, skip_closeout=skip_closeout)

    state, events, completed = HandlerBuildLoop().handle(command)

    # Terminal state reached COMPLETE with one completed cycle, no error.
    assert state.current_phase is EnumBuildLoopPhase.COMPLETE
    assert state.error_message is None
    assert state.cycle_count == 1

    # Completed-event payload reflects the successful cycle.
    assert completed.final_phase is EnumBuildLoopPhase.COMPLETE
    assert completed.cycles_completed == 1
    assert completed.cycles_failed == 0
    assert completed.correlation_id == command.correlation_id

    # Every transition succeeded and ends at COMPLETE.
    assert len(events) >= expected_min_transitions
    assert all(event.success for event in events)
    assert events[-1].to_phase is EnumBuildLoopPhase.COMPLETE

    # skip_closeout must elide the CLOSING_OUT phase from the transition trace.
    if forbid_phase is not None:
        assert all(event.to_phase is not forbid_phase for event in events)


@pytest.mark.integration
def test_build_loop_failing_phase_does_not_complete() -> None:
    """Negative control: a failing phase stalls the FSM with a recorded error."""
    command = _command("build")

    state, events, completed = HandlerBuildLoop().handle(
        command, phase_results={EnumBuildLoopPhase.BUILDING: False}
    )

    # The FSM must NOT report COMPLETE for a failing phase.
    assert state.current_phase is not EnumBuildLoopPhase.COMPLETE
    assert completed.final_phase is not EnumBuildLoopPhase.COMPLETE
    assert completed.cycles_completed == 0

    # The failure is recorded as a finding, not silently swallowed.
    assert state.consecutive_failures >= 1
    assert state.error_message is not None
    assert "failed" in state.error_message.lower()
    assert events[-1].success is False


@pytest.mark.integration
def test_build_loop_circuit_breaker_trips_to_failed() -> None:
    """Three consecutive failures in one phase trip the circuit breaker to FAILED."""
    handler = HandlerBuildLoop()
    command = _command("build")
    state = handler.start(command)
    # Advance IDLE -> CLOSING_OUT successfully so we have a non-terminal phase.
    state, _ = handler.advance(state, phase_success=True)

    last_event = None
    for _ in range(3):
        state, last_event = handler.advance(
            state, phase_success=False, error_message="forced failure"
        )

    assert state.current_phase is EnumBuildLoopPhase.FAILED
    assert state.consecutive_failures == 3
    assert last_event is not None
    assert last_event.to_phase is EnumBuildLoopPhase.FAILED

    completed = handler.make_completed_event(state, command.requested_at)
    assert completed.final_phase is EnumBuildLoopPhase.FAILED
    assert completed.cycles_failed == 1
