"""HandlerRelease — FSM state machine for the release workflow."""

from __future__ import annotations

from omnimarket.nodes.node_release.models.model_release_command import (
    ModelReleaseCommand,
)
from omnimarket.nodes.node_release.models.model_release_state import (
    TERMINAL_PHASES,
    EnumReleasePhase,
    ModelReleaseCompletedEvent,
    ModelReleasePhaseEvent,
    ModelReleaseState,
    next_phase,
)


class HandlerRelease:
    """FSM handler for the release workflow. Pure logic — no external I/O."""

    def start(self, command: ModelReleaseCommand) -> ModelReleaseState:
        """Initialize FSM state from a start command."""
        return ModelReleaseState(
            correlation_id=command.correlation_id,
            current_phase=EnumReleasePhase.IDLE,
            repos=list(command.repos),
            bump=command.bump,
            dry_run=command.dry_run,
        )

    def advance(
        self,
        state: ModelReleaseState,
        phase_success: bool,
        error_message: str | None = None,
        repos_succeeded: int = 0,
        repos_failed: int = 0,
        repos_skipped: int = 0,
    ) -> tuple[ModelReleaseState, ModelReleasePhaseEvent]:
        """Advance the FSM by one phase."""
        from_phase = state.current_phase

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                to_phase = EnumReleasePhase.FAILED
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
            event = ModelReleasePhaseEvent(
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
                "repos_succeeded": state.repos_succeeded + repos_succeeded,
                "repos_failed": state.repos_failed + repos_failed,
                "repos_skipped": state.repos_skipped + repos_skipped,
            }
        )
        event = ModelReleasePhaseEvent(
            correlation_id=state.correlation_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=True,
        )
        return new_state, event

    def make_completed_event(
        self,
        state: ModelReleaseState,
    ) -> ModelReleaseCompletedEvent:
        """Create a completion event from the final state."""
        return ModelReleaseCompletedEvent(
            correlation_id=state.correlation_id,
            final_phase=state.current_phase,
            repos_succeeded=state.repos_succeeded,
            repos_failed=state.repos_failed,
            repos_skipped=state.repos_skipped,
            error_message=state.error_message,
        )

    def run_full_pipeline(
        self,
        command: ModelReleaseCommand,
        phase_results: dict[EnumReleasePhase, bool] | None = None,
    ) -> tuple[
        ModelReleaseState,
        list[ModelReleasePhaseEvent],
        ModelReleaseCompletedEvent,
    ]:
        """Run a complete pipeline through all phases."""
        state = self.start(command)
        events: list[ModelReleasePhaseEvent] = []
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
        command: ModelReleaseCommand,
        phase_results: dict[EnumReleasePhase, bool] | None = None,
    ) -> tuple[
        ModelReleaseState,
        list[ModelReleasePhaseEvent],
        ModelReleaseCompletedEvent,
    ]:
        """Primary entry point — delegates to run_full_pipeline."""
        return self.run_full_pipeline(command, phase_results=phase_results)


__all__: list[str] = ["HandlerRelease"]
