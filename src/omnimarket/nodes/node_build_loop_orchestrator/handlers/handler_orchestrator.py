"""HandlerBuildLoopOrchestrator — orchestrator that drives the build loop FSM.

Receives a start command with a mode (build/close-out/full/observe), creates
initial FSM state, and drives through the mode's phase sequence. For each
phase it emits a phase command intent and collects the result.

ORCHESTRATOR output constraint: emits events[] and intents[], never result.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from omnimarket.nodes.node_build_loop.models.model_loop_state import (
    EnumBuildLoopPhase,
)
from omnimarket.nodes.node_build_loop.models.model_phase_transition_event import (
    ModelPhaseTransitionEvent,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_orchestrator_completed_event import (
    ModelOrchestratorCompletedEvent,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_orchestrator_start_command import (
    ModelOrchestratorStartCommand,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_orchestrator_state import (
    MODE_PHASE_SEQUENCES,
    TERMINAL_ORCHESTRATOR_PHASES,
    EnumOrchestratorPhase,
    ModelOrchestratorState,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_phase_command_intent import (
    ModelPhaseCommandIntent,
)

logger = logging.getLogger(__name__)


class HandlerBuildLoopOrchestrator:
    """Orchestrator that drives the build loop FSM through mode-specific phases.

    Pure orchestration logic. Emits events[] and intents[], never result.
    """

    def start(self, command: ModelOrchestratorStartCommand) -> ModelOrchestratorState:
        """Initialize orchestrator state from a start command."""
        return ModelOrchestratorState(
            correlation_id=command.correlation_id,
            mode=command.mode,
            orchestrator_phase=EnumOrchestratorPhase.IDLE,
            current_build_phase=EnumBuildLoopPhase.IDLE,
            dry_run=command.dry_run,
            max_consecutive_failures=3,
        )

    def advance(
        self,
        state: ModelOrchestratorState,
        phase_success: bool,
        error_message: str | None = None,
    ) -> tuple[ModelOrchestratorState, ModelPhaseTransitionEvent | None]:
        """Advance the orchestrator by one phase.

        On success: moves to the next phase in the mode's sequence.
        On failure: increments consecutive_failures. If threshold hit, -> FAILED.
        Returns updated state and an optional phase transition event.
        """
        if state.orchestrator_phase in TERMINAL_ORCHESTRATOR_PHASES:
            msg = f"Cannot advance from terminal phase: {state.orchestrator_phase}"
            raise ValueError(msg)

        sequence = MODE_PHASE_SEQUENCES[state.mode]
        now = datetime.now(tz=UTC)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                new_state = state.model_copy(
                    update={
                        "orchestrator_phase": EnumOrchestratorPhase.FAILED,
                        "consecutive_failures": new_failures,
                        "error_message": error_message
                        or f"Circuit breaker: {new_failures} consecutive failures",
                    }
                )
                event = ModelPhaseTransitionEvent(
                    correlation_id=state.correlation_id,
                    from_phase=state.current_build_phase,
                    to_phase=EnumBuildLoopPhase.FAILED,
                    success=False,
                    timestamp=now,
                    error_message=error_message,
                )
                return new_state, event

            new_state = state.model_copy(
                update={
                    "consecutive_failures": new_failures,
                    "error_message": error_message,
                }
            )
            event = ModelPhaseTransitionEvent(
                correlation_id=state.correlation_id,
                from_phase=state.current_build_phase,
                to_phase=state.current_build_phase,
                success=False,
                timestamp=now,
                error_message=error_message,
            )
            return new_state, event

        # Success path
        new_index = state.phase_index + 1
        from_build_phase = state.current_build_phase

        if new_index >= len(sequence):
            # All phases in the mode's sequence completed
            new_state = state.model_copy(
                update={
                    "orchestrator_phase": EnumOrchestratorPhase.COMPLETE,
                    "phase_index": new_index,
                    "phases_completed": state.phases_completed + 1,
                    "consecutive_failures": 0,
                    "error_message": None,
                    "current_build_phase": EnumBuildLoopPhase.COMPLETE,
                }
            )
            event = ModelPhaseTransitionEvent(
                correlation_id=state.correlation_id,
                from_phase=from_build_phase,
                to_phase=EnumBuildLoopPhase.COMPLETE,
                success=True,
                timestamp=now,
            )
            return new_state, event

        next_build_phase = sequence[new_index]
        new_state = state.model_copy(
            update={
                "orchestrator_phase": EnumOrchestratorPhase.DRIVING,
                "phase_index": new_index,
                "current_build_phase": next_build_phase,
                "phases_completed": state.phases_completed + 1,
                "consecutive_failures": 0,
                "error_message": None,
            }
        )
        event = ModelPhaseTransitionEvent(
            correlation_id=state.correlation_id,
            from_phase=from_build_phase,
            to_phase=next_build_phase,
            success=True,
            timestamp=now,
        )
        return new_state, event

    def make_phase_intent(
        self, state: ModelOrchestratorState
    ) -> ModelPhaseCommandIntent:
        """Create a phase command intent for the current phase to dispatch."""
        sequence = MODE_PHASE_SEQUENCES[state.mode]
        target = sequence[state.phase_index]
        return ModelPhaseCommandIntent(
            correlation_id=state.correlation_id,
            target_phase=target,
            dry_run=state.dry_run,
            dispatched_at=datetime.now(tz=UTC),
        )

    def make_completed_event(
        self,
        state: ModelOrchestratorState,
        started_at: datetime,
    ) -> ModelOrchestratorCompletedEvent:
        """Create a terminal completed event from the final orchestrator state."""
        return ModelOrchestratorCompletedEvent(
            correlation_id=state.correlation_id,
            mode=state.mode,
            final_phase=state.orchestrator_phase,
            phases_completed=state.phases_completed,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            error_message=state.error_message,
        )

    def serialize_event(self, event: ModelPhaseTransitionEvent) -> bytes:
        """Serialize a phase transition event to bytes for event bus publishing."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def serialize_completed(self, event: ModelOrchestratorCompletedEvent) -> bytes:
        """Serialize a completed event to bytes for event bus publishing."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def serialize_intent(self, intent: ModelPhaseCommandIntent) -> bytes:
        """Serialize a phase command intent to bytes for event bus publishing."""
        return json.dumps(intent.model_dump(mode="json")).encode()

    def run_full_orchestration(
        self,
        command: ModelOrchestratorStartCommand,
        phase_results: dict[EnumBuildLoopPhase, bool] | None = None,
    ) -> tuple[
        ModelOrchestratorState,
        list[ModelPhaseTransitionEvent],
        list[ModelPhaseCommandIntent],
        ModelOrchestratorCompletedEvent,
    ]:
        """Run a complete orchestration through the mode's phase sequence.

        Deterministic entry point for testing. phase_results maps each build
        loop phase to success/failure. If not provided, all phases succeed.

        Returns (final_state, transition_events, command_intents, completed_event).
        """
        started_at = datetime.now(tz=UTC)
        state = self.start(command)
        events: list[ModelPhaseTransitionEvent] = []
        intents: list[ModelPhaseCommandIntent] = []
        results = phase_results or {}

        sequence = MODE_PHASE_SEQUENCES[command.mode]

        # Enter DRIVING state with first phase
        if len(sequence) > 0:
            first_phase = sequence[0]
            state = state.model_copy(
                update={
                    "orchestrator_phase": EnumOrchestratorPhase.DRIVING,
                    "current_build_phase": first_phase,
                }
            )

        while state.orchestrator_phase not in TERMINAL_ORCHESTRATOR_PHASES:
            # Emit intent for the current phase
            intent = self.make_phase_intent(state)
            intents.append(intent)

            # Look up phase result
            target = sequence[state.phase_index]
            success = results.get(target, True)
            error_msg = None if success else f"Phase {target.value} failed"

            state, event = self.advance(
                state,
                phase_success=success,
                error_message=error_msg,
            )
            if event is not None:
                events.append(event)

            # If failure under threshold, break to avoid infinite loop
            if (
                not success
                and state.orchestrator_phase not in TERMINAL_ORCHESTRATOR_PHASES
            ):
                break

        completed = self.make_completed_event(state, started_at)
        return state, events, intents, completed


__all__: list[str] = ["HandlerBuildLoopOrchestrator"]
