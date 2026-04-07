# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build loop state reducer handler — FSM with circuit breaker.

The reducer is the ONLY authority for phase transitions.
Pure function: delta(state, event) -> (new_state, intents[]).

Migrated from omnibase_infra [OMN-7577].

Related:
    - OMN-7313: node_loop_state_reducer
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import logging
from typing import Literal

from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_event import (
    EnumBuildLoopPhase,
    ModelBuildLoopEvent,
)
from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_intent import (
    EnumBuildLoopIntentType,
    ModelBuildLoopIntent,
)
from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_state import (
    ModelBuildLoopState,
)

logger = logging.getLogger(__name__)

# Valid FSM transitions: from_phase -> (success_phase, failure_phase)
_TRANSITIONS: dict[
    EnumBuildLoopPhase,
    tuple[EnumBuildLoopPhase, EnumBuildLoopPhase],
] = {
    EnumBuildLoopPhase.IDLE: (
        EnumBuildLoopPhase.CLOSING_OUT,
        EnumBuildLoopPhase.FAILED,
    ),
    EnumBuildLoopPhase.CLOSING_OUT: (
        EnumBuildLoopPhase.VERIFYING,
        EnumBuildLoopPhase.FAILED,
    ),
    EnumBuildLoopPhase.VERIFYING: (
        EnumBuildLoopPhase.FILLING,
        EnumBuildLoopPhase.FAILED,
    ),
    EnumBuildLoopPhase.FILLING: (
        EnumBuildLoopPhase.CLASSIFYING,
        EnumBuildLoopPhase.FAILED,
    ),
    EnumBuildLoopPhase.CLASSIFYING: (
        EnumBuildLoopPhase.BUILDING,
        EnumBuildLoopPhase.FAILED,
    ),
    EnumBuildLoopPhase.BUILDING: (
        EnumBuildLoopPhase.COMPLETE,
        EnumBuildLoopPhase.FAILED,
    ),
}

# Map phase -> intent type to emit on successful transition
_PHASE_INTENTS: dict[EnumBuildLoopPhase, EnumBuildLoopIntentType] = {
    EnumBuildLoopPhase.CLOSING_OUT: EnumBuildLoopIntentType.START_CLOSEOUT,
    EnumBuildLoopPhase.VERIFYING: EnumBuildLoopIntentType.START_VERIFY,
    EnumBuildLoopPhase.FILLING: EnumBuildLoopIntentType.START_FILL,
    EnumBuildLoopPhase.CLASSIFYING: EnumBuildLoopIntentType.START_CLASSIFY,
    EnumBuildLoopPhase.BUILDING: EnumBuildLoopIntentType.START_BUILD,
    EnumBuildLoopPhase.COMPLETE: EnumBuildLoopIntentType.CYCLE_COMPLETE,
}


# Handler type/category as Literals (replacing omnibase_infra enums)
HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]


class HandlerLoopState:
    """Pure reducer: delta(state, event) -> (new_state, intents).

    Circuit breaker: after max_consecutive_failures, transitions to FAILED.
    Handles duplicate/out-of-order events by checking source_phase against current phase.
    """

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    def delta(
        self,
        state: ModelBuildLoopState,
        event: ModelBuildLoopEvent,
    ) -> tuple[ModelBuildLoopState, list[ModelBuildLoopIntent]]:
        """Compute the next state and intents from current state + event.

        Args:
            state: Current FSM state.
            event: Incoming event.

        Returns:
            Tuple of (new_state, intents_to_emit).
        """
        # Reject duplicate/out-of-order: event source_phase must match current phase
        if event.correlation_id != state.correlation_id:
            logger.warning(
                "Rejecting event: correlation_id mismatch (event=%s, state=%s)",
                event.correlation_id,
                state.correlation_id,
            )
            return state, []

        if event.source_phase != state.phase:
            logger.warning(
                "Rejecting out-of-order event: source_phase=%s but current phase=%s",
                event.source_phase.value,
                state.phase.value,
            )
            return state, []

        # Terminal states reject all events
        if state.phase in (EnumBuildLoopPhase.COMPLETE, EnumBuildLoopPhase.FAILED):
            logger.warning(
                "Rejecting event: already in terminal phase %s", state.phase.value
            )
            return state, []

        transition = _TRANSITIONS.get(state.phase)
        if transition is None:
            logger.error("No transition defined for phase %s", state.phase.value)
            return state, []

        success_phase, failure_phase = transition

        if not event.success:
            new_failures = state.consecutive_failures + 1
            # Circuit breaker check
            if new_failures >= state.max_consecutive_failures:
                logger.error(
                    "Circuit breaker tripped: %d consecutive failures (max=%d)",
                    new_failures,
                    state.max_consecutive_failures,
                )
                new_state = state.model_copy(
                    update={
                        "phase": EnumBuildLoopPhase.FAILED,
                        "consecutive_failures": new_failures,
                        "last_phase_at": event.timestamp,
                        "error_message": f"Circuit breaker: {new_failures} consecutive failures. Last: {event.error_message}",
                    }
                )
                return new_state, [
                    ModelBuildLoopIntent(
                        intent_type=EnumBuildLoopIntentType.CIRCUIT_BREAK,
                        correlation_id=state.correlation_id,
                        cycle_number=state.cycle_number,
                        from_phase=state.phase,
                    )
                ]

            # Non-circuit-break failure: transition to FAILED
            new_state = state.model_copy(
                update={
                    "phase": failure_phase,
                    "consecutive_failures": new_failures,
                    "last_phase_at": event.timestamp,
                    "error_message": event.error_message,
                }
            )
            return new_state, []

        # Success: reset failure count, advance phase
        # Handle skip_closeout: IDLE -> VERIFYING directly
        next_phase = success_phase
        if state.phase == EnumBuildLoopPhase.IDLE and state.skip_closeout:
            next_phase = EnumBuildLoopPhase.VERIFYING

        update: dict[str, object] = {
            "phase": next_phase,
            "consecutive_failures": 0,
            "last_phase_at": event.timestamp,
            "error_message": None,
        }

        # Capture phase-specific data
        if event.tickets_filled > 0:
            update["tickets_filled"] = event.tickets_filled
        if event.tickets_classified > 0:
            update["tickets_classified"] = event.tickets_classified
        if event.tickets_dispatched > 0:
            update["tickets_dispatched"] = event.tickets_dispatched

        # Set started_at on first transition from IDLE
        if state.phase == EnumBuildLoopPhase.IDLE:
            update["started_at"] = event.timestamp
            update["cycle_number"] = state.cycle_number + 1

        new_state = state.model_copy(update=update)

        # Emit intent for the new phase
        intents: list[ModelBuildLoopIntent] = []
        intent_type = _PHASE_INTENTS.get(next_phase)
        if intent_type is not None:
            intents.append(
                ModelBuildLoopIntent(
                    intent_type=intent_type,
                    correlation_id=state.correlation_id,
                    cycle_number=new_state.cycle_number,
                    from_phase=next_phase,
                )
            )

        logger.info(
            "Transition: %s -> %s (cycle=%d, correlation=%s)",
            state.phase.value,
            next_phase.value,
            new_state.cycle_number,
            state.correlation_id,
        )

        return new_state, intents
