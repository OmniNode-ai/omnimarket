"""HandlerTicketWork — FSM state machine for per-ticket execution."""

from __future__ import annotations

from omnimarket.nodes.node_ticket_work.models.model_ticket_work_command import (
    ModelTicketWorkCommand,
)
from omnimarket.nodes.node_ticket_work.models.model_ticket_work_state import (
    TERMINAL_PHASES,
    EnumTicketWorkPhase,
    ModelTicketWorkCompletedEvent,
    ModelTicketWorkPhaseEvent,
    ModelTicketWorkState,
    next_phase,
)


class HandlerTicketWork:
    """FSM handler for per-ticket execution. Pure logic — no external I/O."""

    def start(self, command: ModelTicketWorkCommand) -> ModelTicketWorkState:
        """Initialize FSM state from a start command."""
        return ModelTicketWorkState(
            correlation_id=command.correlation_id,
            current_phase=EnumTicketWorkPhase.IDLE,
            ticket_id=command.ticket_id,
            autonomous=command.autonomous,
            dry_run=command.dry_run,
        )

    def advance(
        self,
        state: ModelTicketWorkState,
        phase_success: bool,
        error_message: str | None = None,
        pr_url: str | None = None,
        commits: list[str] | None = None,
    ) -> tuple[ModelTicketWorkState, ModelTicketWorkPhaseEvent]:
        """Advance the FSM by one phase."""
        from_phase = state.current_phase

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                to_phase = EnumTicketWorkPhase.FAILED
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
            event = ModelTicketWorkPhaseEvent(
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
        }
        if pr_url is not None:
            updates["pr_url"] = pr_url
        if commits is not None:
            updates["commits"] = commits

        new_state = state.model_copy(update=updates)
        event = ModelTicketWorkPhaseEvent(
            correlation_id=state.correlation_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=True,
        )
        return new_state, event

    def make_completed_event(
        self,
        state: ModelTicketWorkState,
    ) -> ModelTicketWorkCompletedEvent:
        """Create a completion event from the final state."""
        return ModelTicketWorkCompletedEvent(
            correlation_id=state.correlation_id,
            final_phase=state.current_phase,
            ticket_id=state.ticket_id,
            pr_url=state.pr_url,
            error_message=state.error_message,
        )

    def run_full_pipeline(
        self,
        command: ModelTicketWorkCommand,
        phase_results: dict[EnumTicketWorkPhase, bool] | None = None,
    ) -> tuple[
        ModelTicketWorkState,
        list[ModelTicketWorkPhaseEvent],
        ModelTicketWorkCompletedEvent,
    ]:
        """Run a complete pipeline through all phases."""
        state = self.start(command)
        events: list[ModelTicketWorkPhaseEvent] = []
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
        command: ModelTicketWorkCommand,
        phase_results: dict[EnumTicketWorkPhase, bool] | None = None,
    ) -> tuple[
        ModelTicketWorkState,
        list[ModelTicketWorkPhaseEvent],
        ModelTicketWorkCompletedEvent,
    ]:
        """Primary entry point — delegates to run_full_pipeline."""
        return self.run_full_pipeline(command, phase_results=phase_results)


__all__: list[str] = ["HandlerTicketWork"]
