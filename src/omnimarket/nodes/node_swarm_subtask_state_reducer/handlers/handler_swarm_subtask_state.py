# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Swarm subtask state reducer handler.

Pure reducer: delta(state, event) -> (new_state, projection_freshness).

Idempotency key: run_id + subtask_id + terminal_event_id.
Ordering authority: topic/partition/offset, not emitted_at timestamps.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_projection_freshness import (
    EnumFreshnessState,
    ModelProjectionFreshness,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_subtask_state import (
    TERMINAL_STATES,
    EnumSubtaskState,
    ModelSubtaskState,
    ModelSwarmRunState,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_swarm_subtask_input import (
    EnumDelegationEventType,
    ModelDelegationEvent,
    ModelSwarmSubtaskReducerInput,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_swarm_subtask_output import (
    ModelSwarmSubtaskReducerOutput,
)

logger = logging.getLogger(__name__)

REDUCER_VERSION = "1.0.0"

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]

_EVENT_TO_STATE: dict[EnumDelegationEventType, EnumSubtaskState] = {
    EnumDelegationEventType.DELEGATION_EXECUTE: EnumSubtaskState.ASSIGNED,
    EnumDelegationEventType.DELEGATION_CALL_COMPLETED: EnumSubtaskState.COMPLETED,
    EnumDelegationEventType.DELEGATION_ESCALATION_TRIGGERED: EnumSubtaskState.ESCALATING,
    EnumDelegationEventType.DELEGATION_ALL_TIERS_FAILED: EnumSubtaskState.FAILED,
}


class HandlerSwarmSubtaskState:
    """Pure reducer for per-subtask lifecycle state.

    Idempotent: replaying a terminal event with the same terminal_event_id
    produces no state change and no duplicate projection-applied emission.
    """

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim."""
        inp = ModelSwarmSubtaskReducerInput(**input_data)
        result = self.delta(inp)
        return result.model_dump(mode="json")

    def delta(
        self,
        inp: ModelSwarmSubtaskReducerInput,
    ) -> ModelSwarmSubtaskReducerOutput:
        """Compute next run state from current state + delegation event.

        Returns unchanged state (state_changed=False) when:
        - event type is swarm_fanout_completed (whole-run terminal, not per-subtask)
        - the subtask is already in a terminal state and this is not a new terminal event
        - idempotency: same terminal_event_id already processed
        """
        event = inp.event
        current = inp.current_state or _empty_run_state(event.run_id)

        # swarm-fanout-completed is whole-run terminal; no per-subtask transition
        if event.event_type == EnumDelegationEventType.SWARM_FANOUT_COMPLETED:
            return ModelSwarmSubtaskReducerOutput(
                new_state=current,
                state_changed=False,
            )

        target_state = _EVENT_TO_STATE.get(event.event_type)
        if target_state is None:
            logger.warning("Unhandled event type: %s", event.event_type)
            return ModelSwarmSubtaskReducerOutput(
                new_state=current,
                state_changed=False,
            )

        existing = current.subtasks.get(event.subtask_id)

        # Idempotency: if subtask is terminal and this event_id already recorded, no-op
        if existing is not None and existing.state in TERMINAL_STATES:
            if existing.terminal_event_id == event.event_id:
                logger.debug(
                    "Idempotent replay: subtask=%s event_id=%s already terminal",
                    event.subtask_id,
                    event.event_id,
                )
                return ModelSwarmSubtaskReducerOutput(
                    new_state=current,
                    state_changed=False,
                )
            # A new terminal event on an already-terminal subtask: log and skip
            # (BLOCKED may arrive after FAILED from a different code path)
            if target_state in TERMINAL_STATES:
                logger.warning(
                    "Subtask %s already terminal (%s); ignoring %s",
                    event.subtask_id,
                    existing.state,
                    target_state,
                )
                return ModelSwarmSubtaskReducerOutput(
                    new_state=current,
                    state_changed=False,
                )

        new_subtask = _build_subtask_state(event, existing, target_state)
        new_subtasks = dict(current.subtasks)
        new_subtasks[event.subtask_id] = new_subtask

        new_run_state = ModelSwarmRunState(
            run_id=current.run_id,
            subtasks=new_subtasks,
            total_count=max(current.total_count, len(new_subtasks)),
        )

        projection_cursor = (
            f"{event.source_topic}/{event.source_partition}/{event.source_offset}"
        )
        freshness = ModelProjectionFreshness(
            projection_cursor=projection_cursor,
            source_event_id=event.event_id,
            source_topic=event.source_topic,
            source_partition=event.source_partition,
            source_offset=event.source_offset,
            freshness_state=EnumFreshnessState.FRESH,
            reducer_version=REDUCER_VERSION,
            observed_at=event.emitted_at,
        )

        logger.info(
            "Subtask transition: run=%s subtask=%s %s -> %s",
            event.run_id,
            event.subtask_id,
            existing.state if existing else "new",
            target_state,
        )

        return ModelSwarmSubtaskReducerOutput(
            new_state=new_run_state,
            changed_subtask=new_subtask,
            state_changed=True,
            projection_freshness=freshness,
        )


def _empty_run_state(run_id: str) -> ModelSwarmRunState:
    return ModelSwarmRunState(run_id=run_id, subtasks={}, total_count=0)


def _build_subtask_state(
    event: ModelDelegationEvent,
    existing: ModelSubtaskState | None,
    target_state: EnumSubtaskState,
) -> ModelSubtaskState:
    attempt_count = (existing.attempt_count if existing else 0) + (
        1
        if event.event_type == EnumDelegationEventType.DELEGATION_ESCALATION_TRIGGERED
        else 0
    )
    if existing is None:
        attempt_count = (
            1 if event.event_type == EnumDelegationEventType.DELEGATION_EXECUTE else 0
        )

    terminal_event_id = (
        event.event_id
        if target_state in TERMINAL_STATES
        else (existing.terminal_event_id if existing else "")
    )

    return ModelSubtaskState(
        run_id=event.run_id,
        subtask_id=event.subtask_id,
        state=target_state,
        endpoint_id=event.endpoint_id or (existing.endpoint_id if existing else ""),
        model_id=event.model_id or (existing.model_id if existing else ""),
        assigned_at=(
            event.emitted_at
            if event.event_type == EnumDelegationEventType.DELEGATION_EXECUTE
            else (existing.assigned_at if existing else "")
        ),
        completed_at=(
            event.emitted_at
            if target_state in TERMINAL_STATES
            else (existing.completed_at if existing else "")
        ),
        latency_ms=existing.latency_ms if existing else 0,
        attempt_count=attempt_count,
        failure_class=event.failure_class
        or (existing.failure_class if existing else ""),
        terminal_event_id=terminal_event_id,
    )
