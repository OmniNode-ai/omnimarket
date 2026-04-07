"""HandlerCloseOut — FSM state machine for the close-out pipeline.

Pure state machine logic. Phases: IDLE -> MERGE_SWEEP -> DEPLOY_PLUGIN ->
START_ENV -> INTEGRATION -> RELEASE_CHECK -> REDEPLOY_CHECK ->
DASHBOARD_SWEEP -> DONE.

Circuit breaker: 3 consecutive failures -> FAILED.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_close_out.models.model_close_out_completed_event import (
    ModelCloseOutCompletedEvent,
)
from omnimarket.nodes.node_close_out.models.model_close_out_phase_event import (
    ModelCloseOutPhaseEvent,
)
from omnimarket.nodes.node_close_out.models.model_close_out_start_command import (
    ModelCloseOutStartCommand,
)
from omnimarket.nodes.node_close_out.models.model_close_out_state import (
    TERMINAL_PHASES,
    EnumCloseOutPhase,
    ModelCloseOutState,
    next_phase,
)

logger = logging.getLogger(__name__)


class HandlerCloseOut:
    """FSM handler for the close-out pipeline.

    Pure logic — no external I/O. Callers wire event bus publish/subscribe.
    """

    def start(self, command: ModelCloseOutStartCommand) -> ModelCloseOutState:
        """Initialize close-out state from a start command."""
        return ModelCloseOutState(
            correlation_id=command.correlation_id,
            current_phase=EnumCloseOutPhase.IDLE,
            dry_run=command.dry_run,
            max_consecutive_failures=3,
        )

    def advance(
        self,
        state: ModelCloseOutState,
        phase_success: bool,
        error_message: str | None = None,
        prs_merged: int = 0,
        prs_polished: int = 0,
    ) -> tuple[ModelCloseOutState, ModelCloseOutPhaseEvent]:
        """Advance the FSM by one phase."""
        from_phase = state.current_phase

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        now = datetime.now(tz=UTC)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                to_phase = EnumCloseOutPhase.FAILED
                err = (
                    error_message
                    or f"Circuit breaker: {new_failures} consecutive failures"
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

            event = ModelCloseOutPhaseEvent(
                correlation_id=state.correlation_id,
                from_phase=from_phase,
                to_phase=to_phase,
                success=False,
                timestamp=now,
                error_message=error_message,
            )
            return new_state, event

        to_phase = next_phase(from_phase)
        new_state = state.model_copy(
            update={
                "current_phase": to_phase,
                "consecutive_failures": 0,
                "error_message": None,
                "prs_merged": state.prs_merged + prs_merged,
                "prs_polished": state.prs_polished + prs_polished,
            }
        )

        event = ModelCloseOutPhaseEvent(
            correlation_id=state.correlation_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=True,
            timestamp=now,
        )
        return new_state, event

    def make_completed_event(
        self,
        state: ModelCloseOutState,
        started_at: datetime,
    ) -> ModelCloseOutCompletedEvent:
        """Create a completion event from the final state."""
        return ModelCloseOutCompletedEvent(
            correlation_id=state.correlation_id,
            final_phase=state.current_phase,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            prs_merged=state.prs_merged,
            prs_polished=state.prs_polished,
            error_message=state.error_message,
        )

    def serialize_event(self, event: ModelCloseOutPhaseEvent) -> bytes:
        """Serialize a phase event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def serialize_completed(self, event: ModelCloseOutCompletedEvent) -> bytes:
        """Serialize a completed event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to run_full_pipeline with a ModelCloseOutStartCommand
        constructed from input_data.
        """
        command = ModelCloseOutStartCommand(**input_data)
        _state, _events, completed = self.run_full_pipeline(command)
        return completed.model_dump(mode="json")

    def run_full_pipeline(
        self,
        command: ModelCloseOutStartCommand,
        phase_results: dict[EnumCloseOutPhase, bool] | None = None,
    ) -> tuple[
        ModelCloseOutState,
        list[ModelCloseOutPhaseEvent],
        ModelCloseOutCompletedEvent,
    ]:
        """Run a complete close-out pipeline with provided results.

        Deterministic entry point for testing.
        """
        started_at = datetime.now(tz=UTC)
        state = self.start(command)
        events: list[ModelCloseOutPhaseEvent] = []
        results = phase_results or {}

        while state.current_phase not in TERMINAL_PHASES:
            target = next_phase(state.current_phase)
            success = results.get(target, True)
            error_msg = None if success else f"Phase {target.value} failed"

            state, event = self.advance(
                state,
                phase_success=success,
                error_message=error_msg,
            )
            events.append(event)

            if not success and state.current_phase not in TERMINAL_PHASES:
                break

        completed = self.make_completed_event(state, started_at)
        return state, events, completed


__all__: list[str] = ["HandlerCloseOut"]
