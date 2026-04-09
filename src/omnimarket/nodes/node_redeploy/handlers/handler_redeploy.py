"""HandlerRedeploy — FSM state machine for the redeploy workflow."""

from __future__ import annotations

from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    ModelRedeployCommand,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_state import (
    TERMINAL_PHASES,
    EnumRedeployPhase,
    ModelRedeployCompletedEvent,
    ModelRedeployPhaseEvent,
    ModelRedeployState,
    next_phase,
)


class HandlerRedeploy:
    """FSM handler for the redeploy workflow. Pure logic — no external I/O."""

    def start(self, command: ModelRedeployCommand) -> ModelRedeployState:
        """Initialize FSM state from a start command."""
        return ModelRedeployState(
            correlation_id=command.correlation_id,
            current_phase=EnumRedeployPhase.IDLE,
            versions=dict(command.versions),
            skip_sync=command.skip_sync,
            verify_only=command.verify_only,
            dry_run=command.dry_run,
        )

    def advance(
        self,
        state: ModelRedeployState,
        phase_success: bool,
        error_message: str | None = None,
    ) -> tuple[ModelRedeployState, ModelRedeployPhaseEvent]:
        """Advance the FSM by one phase."""
        from_phase = state.current_phase

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                to_phase = EnumRedeployPhase.FAILED
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

    def make_completed_event(
        self,
        state: ModelRedeployState,
    ) -> ModelRedeployCompletedEvent:
        """Create a completion event from the final state."""
        return ModelRedeployCompletedEvent(
            correlation_id=state.correlation_id,
            final_phase=state.current_phase,
            phases_completed=state.phases_completed,
            error_message=state.error_message,
        )

    def run_full_pipeline(
        self,
        command: ModelRedeployCommand,
        phase_results: dict[EnumRedeployPhase, bool] | None = None,
    ) -> tuple[
        ModelRedeployState,
        list[ModelRedeployPhaseEvent],
        ModelRedeployCompletedEvent,
    ]:
        """Run a complete pipeline through all phases."""
        state = self.start(command)
        events: list[ModelRedeployPhaseEvent] = []
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
        command: ModelRedeployCommand,
        phase_results: dict[EnumRedeployPhase, bool] | None = None,
    ) -> tuple[
        ModelRedeployState,
        list[ModelRedeployPhaseEvent],
        ModelRedeployCompletedEvent,
    ]:
        """Primary entry point — delegates to run_full_pipeline."""
        return self.run_full_pipeline(command, phase_results=phase_results)


__all__: list[str] = ["HandlerRedeploy"]
