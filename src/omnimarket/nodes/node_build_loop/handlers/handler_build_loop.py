"""HandlerBuildLoop — FSM state machine for the autonomous build loop.

Pure state machine logic. Each phase transition is driven by external
callers feeding phase completion results. The handler does not perform
external I/O — it only manages state transitions, emits phase transition
events, and enforces the circuit breaker.

FSM phases and sequences are mode-dependent (BUILD, CLOSE_OUT, FULL, OBSERVE).
Any phase failure increments consecutive_failures.
3 consecutive failures -> FAILED (circuit breaker).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from omnimarket.nodes.node_build_loop.models.model_loop_completed_event import (
    ModelLoopCompletedEvent,
)
from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
    ModelLoopStartCommand,
)
from omnimarket.nodes.node_build_loop.models.model_loop_state import (
    TERMINAL_PHASES,
    EnumBuildLoopMode,
    EnumBuildLoopPhase,
    ModelLoopState,
    next_phase,
)
from omnimarket.nodes.node_build_loop.models.model_phase_transition_event import (
    ModelPhaseTransitionEvent,
)

logger = logging.getLogger(__name__)


class HandlerBuildLoop:
    """FSM handler for the autonomous build loop.

    Manages state transitions through mode-dependent phase sequences.
    Pure logic — no external I/O. Callers are responsible for wiring
    event bus publish/subscribe.
    """

    def handle(
        self,
        command: ModelLoopStartCommand,
        phase_results: dict[EnumBuildLoopPhase, bool] | None = None,
    ) -> tuple[
        ModelLoopState, list[ModelPhaseTransitionEvent], ModelLoopCompletedEvent
    ]:
        """Primary entry point for RuntimeLocal handler protocol.

        Dispatches a full cycle through the FSM based on the command's mode.
        Delegates to run_full_cycle() which drives phase transitions according
        to the mode (BUILD, CLOSE_OUT, FULL, OBSERVE).

        Args:
            command: Start command with mode, correlation_id, and config.
            phase_results: Optional per-phase success/failure overrides.

        Returns:
            (final_state, transition_events, completed_event).
        """
        return self.run_full_cycle(command, phase_results=phase_results)

    def start(self, command: ModelLoopStartCommand) -> ModelLoopState:
        """Initialize loop state from a start command.

        Returns the initial IDLE state ready for phase progression.
        """
        return ModelLoopState(
            correlation_id=command.correlation_id,
            current_phase=EnumBuildLoopPhase.IDLE,
            mode=EnumBuildLoopMode(command.mode),
            skip_closeout=command.skip_closeout,
            dry_run=command.dry_run,
            max_consecutive_failures=3,
        )

    def advance(
        self,
        state: ModelLoopState,
        phase_success: bool,
        error_message: str | None = None,
        tickets_filled: int = 0,
        tickets_classified: int = 0,
        tickets_dispatched: int = 0,
        verification_snapshot: dict[str, object] | None = None,
    ) -> tuple[ModelLoopState, ModelPhaseTransitionEvent]:
        """Advance the FSM by one phase.

        On success: transitions to the next phase in the sequence.
        On failure: increments consecutive_failures. If threshold hit, -> FAILED.

        If verification_snapshot is provided and we're transitioning from
        VERIFYING, the snapshot is captured in the state for downstream use.
        """
        from_phase = state.current_phase

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        now = datetime.now(tz=UTC)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                # Circuit breaker tripped
                to_phase = EnumBuildLoopPhase.FAILED
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
                # Failure but under threshold — stay in same phase for retry
                to_phase = from_phase
                new_state = state.model_copy(
                    update={
                        "consecutive_failures": new_failures,
                        "error_message": error_message,
                    }
                )

            event = ModelPhaseTransitionEvent(
                correlation_id=state.correlation_id,
                from_phase=from_phase,
                to_phase=to_phase,
                success=False,
                timestamp=now,
                error_message=error_message,
            )
            return new_state, event

        # Success path — advance to next phase (mode-aware)
        to_phase = next_phase(
            from_phase, skip_closeout=state.skip_closeout, mode=state.mode
        )
        updates: dict[str, object] = {
            "current_phase": to_phase,
            "consecutive_failures": 0,
            "error_message": None,
            "tickets_filled": state.tickets_filled + tickets_filled,
            "tickets_classified": state.tickets_classified + tickets_classified,
            "tickets_dispatched": state.tickets_dispatched + tickets_dispatched,
        }

        # Capture verification snapshot when leaving VERIFYING phase
        if (
            from_phase == EnumBuildLoopPhase.VERIFYING
            and verification_snapshot is not None
        ):
            updates["verification_snapshot"] = verification_snapshot

        new_state = state.model_copy(update=updates)

        # If we reached COMPLETE, increment cycle count
        if to_phase == EnumBuildLoopPhase.COMPLETE:
            new_state = new_state.model_copy(
                update={"cycle_count": new_state.cycle_count + 1}
            )

        event = ModelPhaseTransitionEvent(
            correlation_id=state.correlation_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=True,
            timestamp=now,
        )
        return new_state, event

    def make_completed_event(
        self,
        state: ModelLoopState,
        started_at: datetime,
    ) -> ModelLoopCompletedEvent:
        """Create a completion event from the final loop state."""
        return ModelLoopCompletedEvent(
            correlation_id=state.correlation_id,
            final_phase=state.current_phase,
            cycles_completed=state.cycle_count,
            cycles_failed=1 if state.current_phase == EnumBuildLoopPhase.FAILED else 0,
            total_tickets_dispatched=state.tickets_dispatched,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            error_message=state.error_message,
        )

    def serialize_event(self, event: ModelPhaseTransitionEvent) -> bytes:
        """Serialize a phase transition event to bytes for event bus publishing."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def serialize_completed(self, event: ModelLoopCompletedEvent) -> bytes:
        """Serialize a completed event to bytes for event bus publishing."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def run_full_cycle(
        self,
        command: ModelLoopStartCommand,
        phase_results: dict[EnumBuildLoopPhase, bool] | None = None,
    ) -> tuple[
        ModelLoopState, list[ModelPhaseTransitionEvent], ModelLoopCompletedEvent
    ]:
        """Run a complete cycle through all phases with provided results.

        This is the deterministic entry point for testing and standalone execution.
        phase_results maps each phase to success/failure. If not provided,
        all phases succeed.

        Returns (final_state, transition_events, completed_event).
        """
        started_at = datetime.now(tz=UTC)
        state = self.start(command)
        events: list[ModelPhaseTransitionEvent] = []
        results = phase_results or {}

        while state.current_phase not in TERMINAL_PHASES:
            target = next_phase(
                state.current_phase,
                skip_closeout=state.skip_closeout,
                mode=state.mode,
            )
            success = results.get(target, True)
            error_msg = None if success else f"Phase {target.value} failed"

            # Generate default verification snapshot when leaving VERIFYING
            snapshot: dict[str, object] | None = None
            if state.current_phase == EnumBuildLoopPhase.VERIFYING and success:
                snapshot = {
                    "captured_at": datetime.now(tz=UTC).isoformat(),
                    "platform_readiness": "pass",
                    "golden_chain": "pass",
                    "data_flow": "pass",
                }

            state, event = self.advance(
                state,
                phase_success=success,
                error_message=error_msg,
                verification_snapshot=snapshot,
            )
            events.append(event)

            # If advance didn't change phase (failure under threshold), break
            # to avoid infinite loop — caller should retry or inspect state
            if not success and state.current_phase not in TERMINAL_PHASES:
                break

        completed = self.make_completed_event(state, started_at)
        return state, events, completed


__all__: list[str] = ["HandlerBuildLoop"]
