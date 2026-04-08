"""HandlerTicketPipeline — FSM state machine for per-ticket execution pipeline.

Pure state machine logic. Phases: IDLE -> PRE_FLIGHT -> IMPLEMENT ->
LOCAL_REVIEW -> CREATE_PR -> TEST_ITERATE -> CI_WATCH -> PR_REVIEW ->
AUTO_MERGE -> DONE.

Circuit breaker: 3 consecutive failures -> FAILED.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_completed_event import (
    ModelPipelineCompletedEvent,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_phase_event import (
    ModelPipelinePhaseEvent,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_start_command import (
    ModelPipelineStartCommand,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_state import (
    TERMINAL_PHASES,
    EnumPipelinePhase,
    ModelPipelineState,
    next_phase,
)

logger = logging.getLogger(__name__)


class HandlerTicketPipeline:
    """FSM handler for the per-ticket execution pipeline.

    Pure logic — no external I/O. Callers wire event bus publish/subscribe.
    """

    def start(self, command: ModelPipelineStartCommand) -> ModelPipelineState:
        """Initialize pipeline state from a start command."""
        return ModelPipelineState(
            correlation_id=command.correlation_id,
            ticket_id=command.ticket_id,
            current_phase=EnumPipelinePhase.IDLE,
            skip_test_iterate=command.skip_test_iterate,
            dry_run=command.dry_run,
            max_consecutive_failures=3,
        )

    def advance(
        self,
        state: ModelPipelineState,
        phase_success: bool,
        error_message: str | None = None,
        pr_number: int | None = None,
    ) -> tuple[ModelPipelineState, ModelPipelinePhaseEvent]:
        """Advance the FSM by one phase."""
        from_phase = state.current_phase

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        now = datetime.now(tz=UTC)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                to_phase = EnumPipelinePhase.FAILED
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

            event = ModelPipelinePhaseEvent(
                correlation_id=state.correlation_id,
                ticket_id=state.ticket_id,
                from_phase=from_phase,
                to_phase=to_phase,
                success=False,
                timestamp=now,
                error_message=error_message,
            )
            return new_state, event

        to_phase = next_phase(from_phase, skip_test_iterate=state.skip_test_iterate)

        updates: dict[str, object] = {
            "current_phase": to_phase,
            "consecutive_failures": 0,
            "error_message": None,
        }
        if pr_number is not None:
            updates["pr_number"] = pr_number

        new_state = state.model_copy(update=updates)

        event = ModelPipelinePhaseEvent(
            correlation_id=state.correlation_id,
            ticket_id=state.ticket_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=True,
            timestamp=now,
        )
        return new_state, event

    def make_completed_event(
        self,
        state: ModelPipelineState,
        started_at: datetime,
    ) -> ModelPipelineCompletedEvent:
        """Create a completion event from the final pipeline state."""
        return ModelPipelineCompletedEvent(
            correlation_id=state.correlation_id,
            ticket_id=state.ticket_id,
            final_phase=state.current_phase,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            pr_number=state.pr_number,
            error_message=state.error_message,
        )

    def serialize_event(self, event: ModelPipelinePhaseEvent) -> bytes:
        """Serialize a phase event to bytes for event bus publishing."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def serialize_completed(self, event: ModelPipelineCompletedEvent) -> bytes:
        """Serialize a completed event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def handle(self, command: ModelPipelineStartCommand) -> ModelPipelineCompletedEvent:
        """Typed RuntimeLocal handler protocol entry point.

        Delegates to run_full_pipeline with the provided typed command.
        """
        _state, _events, completed = self.run_full_pipeline(command)
        return completed

    def run_full_pipeline(
        self,
        command: ModelPipelineStartCommand,
        phase_results: dict[EnumPipelinePhase, bool] | None = None,
    ) -> tuple[
        ModelPipelineState,
        list[ModelPipelinePhaseEvent],
        ModelPipelineCompletedEvent,
    ]:
        """Run a complete pipeline through all phases with provided results.

        Deterministic entry point for testing. phase_results maps each phase
        to success/failure. If not provided, all phases succeed.
        """
        started_at = datetime.now(tz=UTC)
        state = self.start(command)
        events: list[ModelPipelinePhaseEvent] = []
        results = phase_results or {}

        while state.current_phase not in TERMINAL_PHASES:
            target = next_phase(
                state.current_phase,
                skip_test_iterate=state.skip_test_iterate,
            )
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


__all__: list[str] = ["HandlerTicketPipeline"]
