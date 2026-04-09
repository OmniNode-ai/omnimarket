"""HandlerDesignToPlan — FSM state machine for the design-to-plan workflow."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_command import (
    ModelDesignToPlanCommand,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_state import (
    TERMINAL_PHASES,
    EnumDesignToPlanPhase,
    ModelDesignToPlanCompletedEvent,
    ModelDesignToPlanPhaseEvent,
    ModelDesignToPlanState,
    next_phase,
)


class HandlerDesignToPlan:
    """FSM handler for the design-to-plan workflow.

    Pure state machine — no external I/O. Callers drive phase transitions
    by calling advance() with phase results.
    """

    def start(self, command: ModelDesignToPlanCommand) -> ModelDesignToPlanState:
        """Initialize FSM state from a start command."""
        return ModelDesignToPlanState(
            correlation_id=command.correlation_id,
            current_phase=EnumDesignToPlanPhase.IDLE,
            topic=command.topic,
            plan_path=str(command.plan_path) if command.plan_path else None,
            dry_run=command.dry_run,
            no_launch=command.no_launch,
        )

    def advance(
        self,
        state: ModelDesignToPlanState,
        phase_success: bool,
        error_message: str | None = None,
        plan_path: str | None = None,
        review_rounds: int = 0,
    ) -> tuple[ModelDesignToPlanState, ModelDesignToPlanPhaseEvent]:
        """Advance the FSM by one phase.

        On success: transitions to the next phase.
        On failure: increments consecutive_failures. At threshold -> FAILED.
        """
        from_phase = state.current_phase

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                to_phase = EnumDesignToPlanPhase.FAILED
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
            event = ModelDesignToPlanPhaseEvent(
                correlation_id=state.correlation_id,
                from_phase=from_phase,
                to_phase=to_phase,
                success=False,
                error_message=error_message,
            )
            return new_state, event

        to_phase = next_phase(from_phase)
        updates: dict[str, object] = {
            "current_phase": to_phase,
            "consecutive_failures": 0,
            "error_message": None,
            "review_rounds": state.review_rounds + review_rounds,
        }
        if plan_path is not None:
            updates["plan_path"] = plan_path

        new_state = state.model_copy(update=updates)
        event = ModelDesignToPlanPhaseEvent(
            correlation_id=state.correlation_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=True,
        )
        return new_state, event

    def make_completed_event(
        self,
        state: ModelDesignToPlanState,
    ) -> ModelDesignToPlanCompletedEvent:
        """Create a completion event from the final state."""
        return ModelDesignToPlanCompletedEvent(
            correlation_id=state.correlation_id,
            final_phase=state.current_phase,
            plan_path=state.plan_path,
            error_message=state.error_message,
        )

    def serialize_event(self, event: ModelDesignToPlanPhaseEvent) -> bytes:
        """Serialize a phase event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def run_full_pipeline(
        self,
        command: ModelDesignToPlanCommand,
        phase_results: dict[EnumDesignToPlanPhase, bool] | None = None,
    ) -> tuple[
        ModelDesignToPlanState,
        list[ModelDesignToPlanPhaseEvent],
        ModelDesignToPlanCompletedEvent,
    ]:
        """Run a complete pipeline through all phases.

        phase_results maps each phase to success/failure. If not provided,
        all phases succeed.
        """
        state = self.start(command)
        events: list[ModelDesignToPlanPhaseEvent] = []
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

        completed = self.make_completed_event(state)
        return state, events, completed

    def handle(
        self,
        command: ModelDesignToPlanCommand,
        phase_results: dict[EnumDesignToPlanPhase, bool] | None = None,
    ) -> tuple[
        ModelDesignToPlanState,
        list[ModelDesignToPlanPhaseEvent],
        ModelDesignToPlanCompletedEvent,
    ]:
        """Primary entry point — delegates to run_full_pipeline."""
        return self.run_full_pipeline(command, phase_results=phase_results)

    def _started_at(self) -> datetime:
        return datetime.now(tz=UTC)


__all__: list[str] = ["HandlerDesignToPlan"]
