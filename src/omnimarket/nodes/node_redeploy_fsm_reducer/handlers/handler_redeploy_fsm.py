# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Redeploy FSM REDUCER handler (OMN-13211 / B3).

Re-expresses the ``node_redeploy`` ``HandlerRedeploy`` FSM as a canonical
REDUCER node. Pure fold: ``(state, phase-advance event) -> new state``. It owns
the phase sequence, the phase-transition event, and the 3-failure circuit
breaker. No I/O, no bus, no DB — the reducer only updates the FSM state
projection; the orchestrator publishes the transition events over the bus.

Dispatch:
  The runtime delivers a ``ModelEventEnvelope`` whose payload is a
  ``ModelRedeployAdvanceCommand``. The handler returns
  ``ModelHandlerOutput.for_reducer`` carrying the advanced ``ModelRedeployState``
  as its projection.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    TERMINAL_PHASES,
    EnumRedeployPhase,
    ModelRedeployCommand,
    ModelRedeployCompletedEvent,
    ModelRedeployPhaseEvent,
    ModelRedeployState,
    next_phase,
)
from omnimarket.nodes.node_redeploy_fsm_reducer.models.model_redeploy_advance_command import (
    ModelRedeployAdvanceCommand,
)

HANDLER_ID = "redeploy-fsm-reducer"


def start_state(command: ModelRedeployCommand) -> ModelRedeployState:
    """Initialize the FSM state projection from a redeploy start command."""
    return ModelRedeployState(
        correlation_id=command.correlation_id,
        current_phase=EnumRedeployPhase.IDLE,
        versions=dict(command.versions),
        skip_sync=command.skip_sync,
        verify_only=command.verify_only,
        dry_run=command.dry_run,
        runtime_lane=command.runtime_lane,
        image_digest=command.image_digest,
        promotion_batch_id=command.promotion_batch_id,
    )


def advance(
    state: ModelRedeployState,
    phase_success: bool,
    error_message: str | None = None,
) -> tuple[ModelRedeployState, ModelRedeployPhaseEvent]:
    """Advance the FSM by one phase. Pure fold.

    On failure the circuit breaker trips to ``FAILED`` after
    ``max_consecutive_failures`` consecutive failures; otherwise the phase is
    retried in place with the failure counter incremented. On success the FSM
    moves to ``next_phase`` and the failure counter resets.
    """
    from_phase = state.current_phase

    if from_phase in TERMINAL_PHASES:
        msg = f"Cannot advance from terminal phase: {from_phase}"
        raise ValueError(msg)

    if not phase_success:
        new_failures = state.consecutive_failures + 1
        if new_failures >= state.max_consecutive_failures:
            to_phase = EnumRedeployPhase.FAILED
            err = (
                error_message or f"Circuit breaker: {new_failures} consecutive failures"
            )
            new_state = state.model_copy(
                update={
                    "current_phase": to_phase,
                    "consecutive_failures": new_failures,
                    "error_message": err,
                }
            )
        else:
            to_phase = from_phase
            new_state = state.model_copy(
                update={
                    "consecutive_failures": new_failures,
                    "error_message": error_message,
                }
            )
        event = ModelRedeployPhaseEvent(
            correlation_id=state.correlation_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=False,
            error_message=error_message,
        )
        return new_state, event

    to_phase = next_phase(from_phase)
    new_state = state.model_copy(
        update={
            "current_phase": to_phase,
            "consecutive_failures": 0,
            "error_message": None,
            "phases_completed": state.phases_completed + 1,
        }
    )
    event = ModelRedeployPhaseEvent(
        correlation_id=state.correlation_id,
        from_phase=from_phase,
        to_phase=to_phase,
        success=True,
    )
    return new_state, event


def make_completed_event(state: ModelRedeployState) -> ModelRedeployCompletedEvent:
    """Create a completion event from a terminal FSM state."""
    return ModelRedeployCompletedEvent(
        correlation_id=state.correlation_id,
        final_phase=state.current_phase,
        phases_completed=state.phases_completed,
        error_message=state.error_message,
    )


class HandlerRedeployFsm:
    """Pure reducer: fold one phase-advance event into the FSM state projection."""

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Advance the FSM by one phase and emit the new state as a projection."""
        command = _coerce_command(envelope.payload)
        new_state, _event = advance(
            command.state,
            phase_success=command.phase_success,
            error_message=command.error_message,
        )
        return ModelHandlerOutput.for_reducer(
            input_envelope_id=envelope.envelope_id,
            correlation_id=(
                envelope.correlation_id or command.state.correlation_id or uuid4()
            ),
            handler_id=HANDLER_ID,
            projections=(new_state,),
        )


def _coerce_command(payload: Any) -> ModelRedeployAdvanceCommand:
    """Coerce the dispatched payload into a ``ModelRedeployAdvanceCommand``."""
    if isinstance(payload, ModelRedeployAdvanceCommand):
        return payload
    if isinstance(payload, Mapping):
        return ModelRedeployAdvanceCommand.model_validate(dict(payload))
    if hasattr(payload, "model_dump"):
        return ModelRedeployAdvanceCommand.model_validate(payload.model_dump())
    raise TypeError(
        f"redeploy FSM payload must be ModelRedeployAdvanceCommand or a mapping; "
        f"got {type(payload).__name__}"
    )


__all__: list[str] = [
    "HANDLER_ID",
    "HandlerRedeployFsm",
    "advance",
    "make_completed_event",
    "start_state",
]
