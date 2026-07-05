# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerSwarmSubtaskState reducer.

Covers:
- State transitions (assigned → executing → completed/failed/escalating)
- Idempotency: replaying same terminal event is a no-op
- Derived counters are computed from subtasks, not stored
- Escalation is non-terminal (ESCALATING does not block further transitions)
- BLOCKED state on dependency failure
- swarm-fanout-completed is a no-op for per-subtask state
- New subtask from delegation-execute command
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_swarm_subtask_state_reducer.handlers.handler_swarm_subtask_state import (
    HandlerSwarmSubtaskState,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_subtask_state import (
    EnumSubtaskState,
    ModelSubtaskState,
    ModelSwarmRunState,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_swarm_subtask_input import (
    EnumDelegationEventType,
    ModelDelegationEvent,
    ModelSwarmSubtaskReducerInput,
)


def _event(
    event_type: EnumDelegationEventType,
    *,
    run_id: str = "run-1",
    subtask_id: str = "sub-1",
    event_id: str = "evt-1",
    endpoint_id: str = "ep-1",
    model_id: str = "model-1",
    failure_class: str = "",
    source_topic: str = "onex.evt.omnimarket.delegation-call-completed.v1",
    source_partition: int = 0,
    source_offset: int = 1,
    emitted_at: str = "2026-05-25T00:00:00Z",
) -> ModelDelegationEvent:
    return ModelDelegationEvent(
        event_id=event_id,
        event_type=event_type,
        run_id=run_id,
        subtask_id=subtask_id,
        correlation_id=f"{run_id}-{subtask_id}",
        endpoint_id=endpoint_id,
        model_id=model_id,
        emitted_at=emitted_at,
        failure_class=failure_class,
        source_topic=source_topic,
        source_partition=source_partition,
        source_offset=source_offset,
    )


def _inp(
    event: ModelDelegationEvent,
    current_state: ModelSwarmRunState | None = None,
) -> ModelSwarmSubtaskReducerInput:
    return ModelSwarmSubtaskReducerInput(event=event, current_state=current_state)


@pytest.fixture
def handler() -> HandlerSwarmSubtaskState:
    return HandlerSwarmSubtaskState()


@pytest.mark.unit
def test_delegation_execute_creates_assigned_subtask(
    handler: HandlerSwarmSubtaskState,
) -> None:
    evt = _event(
        EnumDelegationEventType.DELEGATION_EXECUTE,
        source_topic="onex.cmd.omnimarket.delegation-execute.v1",
        source_offset=0,
    )
    result = handler.delta(_inp(evt))

    assert result.state_changed is True
    assert result.new_state.run_id == "run-1"
    assert "sub-1" in result.new_state.subtasks
    sub = result.new_state.subtasks["sub-1"]
    assert sub.state == EnumSubtaskState.ASSIGNED
    assert sub.attempt_count == 1
    assert sub.assigned_at == "2026-05-25T00:00:00Z"


@pytest.mark.unit
def test_delegation_call_completed_transitions_to_completed(
    handler: HandlerSwarmSubtaskState,
) -> None:
    # First assign
    assign_evt = _event(
        EnumDelegationEventType.DELEGATION_EXECUTE,
        event_id="evt-assign",
        source_topic="onex.cmd.omnimarket.delegation-execute.v1",
        source_offset=0,
    )
    state_after_assign = handler.delta(_inp(assign_evt)).new_state

    # Then complete
    complete_evt = _event(
        EnumDelegationEventType.DELEGATION_CALL_COMPLETED,
        event_id="evt-complete",
        source_offset=2,
    )
    result = handler.delta(_inp(complete_evt, state_after_assign))

    assert result.state_changed is True
    sub = result.new_state.subtasks["sub-1"]
    assert sub.state == EnumSubtaskState.COMPLETED
    assert sub.terminal_event_id == "evt-complete"
    assert sub.completed_at == "2026-05-25T00:00:00Z"


@pytest.mark.unit
def test_delegation_all_tiers_failed_transitions_to_failed(
    handler: HandlerSwarmSubtaskState,
) -> None:
    assign_evt = _event(
        EnumDelegationEventType.DELEGATION_EXECUTE,
        event_id="evt-assign",
        source_topic="onex.cmd.omnimarket.delegation-execute.v1",
        source_offset=0,
    )
    state_after_assign = handler.delta(_inp(assign_evt)).new_state

    fail_evt = _event(
        EnumDelegationEventType.DELEGATION_ALL_TIERS_FAILED,
        event_id="evt-fail",
        failure_class="all_tiers_exhausted",
        source_offset=3,
    )
    result = handler.delta(_inp(fail_evt, state_after_assign))

    assert result.state_changed is True
    sub = result.new_state.subtasks["sub-1"]
    assert sub.state == EnumSubtaskState.FAILED
    assert sub.terminal_event_id == "evt-fail"
    assert sub.failure_class == "all_tiers_exhausted"


@pytest.mark.unit
def test_escalation_is_non_terminal(
    handler: HandlerSwarmSubtaskState,
) -> None:
    assign_evt = _event(
        EnumDelegationEventType.DELEGATION_EXECUTE,
        event_id="evt-assign",
        source_topic="onex.cmd.omnimarket.delegation-execute.v1",
        source_offset=0,
    )
    state_after_assign = handler.delta(_inp(assign_evt)).new_state

    escalate_evt = _event(
        EnumDelegationEventType.DELEGATION_ESCALATION_TRIGGERED,
        event_id="evt-escalate",
        source_offset=1,
    )
    result = handler.delta(_inp(escalate_evt, state_after_assign))

    assert result.state_changed is True
    sub = result.new_state.subtasks["sub-1"]
    assert sub.state == EnumSubtaskState.ESCALATING
    # Non-terminal: terminal_event_id must not be set
    assert sub.terminal_event_id == ""
    # attempt_count incremented on escalation
    assert sub.attempt_count == 2

    # Can still transition to COMPLETED after escalation
    complete_evt = _event(
        EnumDelegationEventType.DELEGATION_CALL_COMPLETED,
        event_id="evt-complete",
        source_offset=4,
    )
    final = handler.delta(_inp(complete_evt, result.new_state))
    assert final.state_changed is True
    assert final.new_state.subtasks["sub-1"].state == EnumSubtaskState.COMPLETED


@pytest.mark.unit
def test_idempotency_replay_same_terminal_event_is_noop(
    handler: HandlerSwarmSubtaskState,
) -> None:
    assign_evt = _event(
        EnumDelegationEventType.DELEGATION_EXECUTE,
        event_id="evt-assign",
        source_topic="onex.cmd.omnimarket.delegation-execute.v1",
        source_offset=0,
    )
    state_after_assign = handler.delta(_inp(assign_evt)).new_state

    complete_evt = _event(
        EnumDelegationEventType.DELEGATION_CALL_COMPLETED,
        event_id="evt-complete",
        source_offset=2,
    )
    state_after_complete = handler.delta(
        _inp(complete_evt, state_after_assign)
    ).new_state

    # Replay the exact same terminal event
    replay_result = handler.delta(_inp(complete_evt, state_after_complete))

    assert replay_result.state_changed is False
    assert replay_result.new_state is state_after_complete


@pytest.mark.unit
def test_derived_counters_computed_from_subtasks(
    handler: HandlerSwarmSubtaskState,
) -> None:
    run_id = "run-counters"
    subtasks: dict[str, ModelSubtaskState] = {
        "s1": ModelSubtaskState(
            run_id=run_id,
            subtask_id="s1",
            state=EnumSubtaskState.COMPLETED,
            terminal_event_id="e1",
        ),
        "s2": ModelSubtaskState(
            run_id=run_id,
            subtask_id="s2",
            state=EnumSubtaskState.FAILED,
            terminal_event_id="e2",
        ),
        "s3": ModelSubtaskState(
            run_id=run_id,
            subtask_id="s3",
            state=EnumSubtaskState.ASSIGNED,
        ),
        "s4": ModelSubtaskState(
            run_id=run_id,
            subtask_id="s4",
            state=EnumSubtaskState.ESCALATING,
        ),
        "s5": ModelSubtaskState(
            run_id=run_id,
            subtask_id="s5",
            state=EnumSubtaskState.BLOCKED,
            terminal_event_id="e5",
        ),
    }
    run_state = ModelSwarmRunState(run_id=run_id, subtasks=subtasks, total_count=5)

    assert run_state.completed_count == 1
    assert run_state.failed_count == 1
    assert run_state.pending_count == 2  # ASSIGNED + ESCALATING
    assert run_state.blocked_count == 1

    # Adding a new completed subtask must update derived counters
    complete_evt = _event(
        EnumDelegationEventType.DELEGATION_CALL_COMPLETED,
        run_id=run_id,
        subtask_id="s3",
        event_id="evt-s3-complete",
        source_offset=10,
    )
    result = handler.delta(_inp(complete_evt, run_state))
    assert result.state_changed is True
    assert result.new_state.completed_count == 2
    assert (
        result.new_state.pending_count == 1
    )  # only s4 (ESCALATING) remains non-terminal


@pytest.mark.unit
def test_blocked_state_on_dependency_failure(
    handler: HandlerSwarmSubtaskState,
) -> None:
    """BLOCKED is emitted as a synthetic terminal event_type=DELEGATION_ALL_TIERS_FAILED
    with failure_class='prerequisite_failed'. Handler maps it to FAILED initially,
    but callers can inject pre-built BLOCKED subtasks directly."""
    run_id = "run-blocked"
    # Simulate a BLOCKED subtask injected directly into run state
    subtasks = {
        "dep-1": ModelSubtaskState(
            run_id=run_id,
            subtask_id="dep-1",
            state=EnumSubtaskState.FAILED,
            terminal_event_id="e-dep",
        ),
        "dep-2": ModelSubtaskState(
            run_id=run_id,
            subtask_id="dep-2",
            state=EnumSubtaskState.BLOCKED,
            terminal_event_id="e-blocked",
        ),
    }
    run_state = ModelSwarmRunState(run_id=run_id, subtasks=subtasks, total_count=2)

    assert run_state.failed_count == 1  # only FAILED, not BLOCKED
    assert run_state.blocked_count == 1
    # Both are terminal so pending_count == 0
    assert run_state.pending_count == 0


@pytest.mark.unit
def test_swarm_fanout_completed_is_noop(
    handler: HandlerSwarmSubtaskState,
) -> None:
    current = ModelSwarmRunState(run_id="run-1", subtasks={}, total_count=0)
    evt = _event(
        EnumDelegationEventType.SWARM_FANOUT_COMPLETED,
        event_id="evt-fanout",
        source_topic="onex.evt.omnimarket.swarm-fanout-completed.v1",
    )
    result = handler.delta(_inp(evt, current))
    assert result.state_changed is False
    assert result.new_state is current


@pytest.mark.unit
def test_projection_freshness_populated_on_state_change(
    handler: HandlerSwarmSubtaskState,
) -> None:
    evt = _event(
        EnumDelegationEventType.DELEGATION_EXECUTE,
        event_id="evt-1",
        source_topic="onex.cmd.omnimarket.delegation-execute.v1",
        source_partition=2,
        source_offset=42,
    )
    result = handler.delta(_inp(evt))

    assert result.state_changed is True
    assert result.projection_freshness is not None
    f = result.projection_freshness
    assert f.source_event_id == "evt-1"
    assert f.source_topic == "onex.cmd.omnimarket.delegation-execute.v1"
    assert f.source_partition == 2
    assert f.source_offset == 42
    assert f.projection_cursor == "onex.cmd.omnimarket.delegation-execute.v1/2/42"
    assert f.reducer_version == "1.0.0"


@pytest.mark.unit
def test_multiple_subtasks_independent_state(
    handler: HandlerSwarmSubtaskState,
) -> None:
    run_id = "run-multi"

    # Assign two subtasks
    assign_1 = _event(
        EnumDelegationEventType.DELEGATION_EXECUTE,
        run_id=run_id,
        subtask_id="s1",
        event_id="e-a1",
        source_topic="onex.cmd.omnimarket.delegation-execute.v1",
        source_offset=0,
    )
    state = handler.delta(_inp(assign_1)).new_state

    assign_2 = _event(
        EnumDelegationEventType.DELEGATION_EXECUTE,
        run_id=run_id,
        subtask_id="s2",
        event_id="e-a2",
        source_topic="onex.cmd.omnimarket.delegation-execute.v1",
        source_offset=1,
    )
    state = handler.delta(_inp(assign_2, state)).new_state

    assert state.pending_count == 2

    # Complete s1
    complete_1 = _event(
        EnumDelegationEventType.DELEGATION_CALL_COMPLETED,
        run_id=run_id,
        subtask_id="s1",
        event_id="e-c1",
        source_offset=2,
    )
    state = handler.delta(_inp(complete_1, state)).new_state

    assert state.completed_count == 1
    assert state.pending_count == 1
    assert state.subtasks["s2"].state == EnumSubtaskState.ASSIGNED

    # Fail s2
    fail_2 = _event(
        EnumDelegationEventType.DELEGATION_ALL_TIERS_FAILED,
        run_id=run_id,
        subtask_id="s2",
        event_id="e-f2",
        source_offset=3,
    )
    state = handler.delta(_inp(fail_2, state)).new_state

    assert state.completed_count == 1
    assert state.failed_count == 1
    assert state.pending_count == 0
