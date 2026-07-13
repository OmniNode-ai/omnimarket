# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Top-level FSM state-coverage companion for node_swarm_subtask_state_reducer.

OMN-14534: the node's real, substantive tests are co-located under
src/omnimarket/nodes/node_swarm_subtask_state_reducer/tests/ (13 pre-existing
tests + the OMN-14534 cross-boundary seam suite). scripts/validate_state_coverage.py
(the state-coverage-gate) only scans REPO_ROOT/tests, so it cannot see
co-located node tests — this node was already listed, pre-existing, in
scripts/validation/state_coverage_baseline.txt for all 4 declared FSM states
before this ticket touched it. Strict mode promotes a baselined gap to FAIL
for any node directly touched by a diff (by design — a grandfather clause for
untouched legacy debt, not a standing exemption), so OMN-14534 closes it here
rather than riding the debt further.

This file asserts the same 4 FSM states the co-located suite already proves;
it is a real (if minimal) exercise of the handler, not a stub.
"""

from __future__ import annotations

from omnimarket.nodes.node_swarm_subtask_state_reducer.handlers.handler_swarm_subtask_state import (
    HandlerSwarmSubtaskState,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_subtask_state import (
    EnumSubtaskState,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_swarm_subtask_input import (
    EnumDelegationEventType,
    ModelDelegationEvent,
    ModelSwarmSubtaskReducerInput,
)


def _event(
    event_type: EnumDelegationEventType, *, event_id: str, failure_class: str = ""
) -> ModelDelegationEvent:
    return ModelDelegationEvent(
        event_id=event_id,
        event_type=event_type,
        run_id="run-fsm-coverage",
        subtask_id="sub-fsm-coverage",
        correlation_id="run-fsm-coverage-sub-fsm-coverage",
        failure_class=failure_class,
    )


def test_delegation_execute_reaches_assigned() -> None:
    handler = HandlerSwarmSubtaskState()
    out = handler.delta(
        ModelSwarmSubtaskReducerInput(
            event=_event(EnumDelegationEventType.DELEGATION_EXECUTE, event_id="e1")
        )
    )
    assert out.changed_subtask is not None
    # String-literal form (EnumSubtaskState is a StrEnum, so this is a real
    # equality check, not just gate bait) — validate_state_coverage.py's AST
    # scan matches quoted-literal Constants; this is the form the FSM state
    # names ("assigned"/"escalating"/"completed"/"failed") declared in
    # contract.yaml's state_machine block are covered by.
    assert out.changed_subtask.state == "assigned"
    assert out.changed_subtask.state == EnumSubtaskState.ASSIGNED


def test_delegation_escalation_triggered_reaches_escalating() -> None:
    handler = HandlerSwarmSubtaskState()
    assigned = handler.delta(
        ModelSwarmSubtaskReducerInput(
            event=_event(EnumDelegationEventType.DELEGATION_EXECUTE, event_id="e1")
        )
    )
    out = handler.delta(
        ModelSwarmSubtaskReducerInput(
            event=_event(
                EnumDelegationEventType.DELEGATION_ESCALATION_TRIGGERED, event_id="e2"
            ),
            current_state=assigned.new_state,
        )
    )
    assert out.changed_subtask is not None
    assert out.changed_subtask.state == "escalating"
    assert out.changed_subtask.state == EnumSubtaskState.ESCALATING


def test_delegation_call_completed_reaches_completed() -> None:
    handler = HandlerSwarmSubtaskState()
    out = handler.delta(
        ModelSwarmSubtaskReducerInput(
            event=_event(
                EnumDelegationEventType.DELEGATION_CALL_COMPLETED, event_id="e1"
            )
        )
    )
    assert out.changed_subtask is not None
    assert out.changed_subtask.state == "completed"
    assert out.changed_subtask.state == EnumSubtaskState.COMPLETED


def test_delegation_all_tiers_failed_reaches_failed() -> None:
    handler = HandlerSwarmSubtaskState()
    out = handler.delta(
        ModelSwarmSubtaskReducerInput(
            event=_event(
                EnumDelegationEventType.DELEGATION_ALL_TIERS_FAILED,
                event_id="e1",
                failure_class="timeout",
            )
        )
    )
    assert out.changed_subtask is not None
    assert out.changed_subtask.state == "failed"
    assert out.changed_subtask.state == EnumSubtaskState.FAILED
