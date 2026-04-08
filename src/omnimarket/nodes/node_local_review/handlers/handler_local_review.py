"""HandlerLocalReview — FSM state machine for iterative local code review.

Pure state machine logic. Phases: INIT -> REVIEW -> FIX -> COMMIT ->
CHECK_CLEAN -> DONE (or loop back to REVIEW if not clean).

Circuit breaker: 3 consecutive failures -> FAILED.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from omnimarket.nodes.node_local_review.models.model_local_review_completed_event import (
    ModelLocalReviewCompletedEvent,
)
from omnimarket.nodes.node_local_review.models.model_local_review_phase_event import (
    ModelLocalReviewPhaseEvent,
)
from omnimarket.nodes.node_local_review.models.model_local_review_start_command import (
    ModelLocalReviewStartCommand,
)
from omnimarket.nodes.node_local_review.models.model_local_review_state import (
    TERMINAL_PHASES,
    EnumLocalReviewPhase,
    ModelLocalReviewState,
    next_phase,
)

logger = logging.getLogger(__name__)


class HandlerLocalReview:
    """FSM handler for the local review loop.

    Pure logic — no external I/O. Callers wire event bus publish/subscribe.
    """

    def start(self, command: ModelLocalReviewStartCommand) -> ModelLocalReviewState:
        """Initialize review state from a start command."""
        return ModelLocalReviewState(
            correlation_id=command.correlation_id,
            current_phase=EnumLocalReviewPhase.INIT,
            dry_run=command.dry_run,
            max_consecutive_failures=3,
            max_iterations=command.max_iterations,
            required_clean_runs=command.required_clean_runs,
        )

    def advance(
        self,
        state: ModelLocalReviewState,
        phase_success: bool,
        error_message: str | None = None,
        is_clean: bool = False,
        issues_found: int = 0,
        issues_fixed: int = 0,
    ) -> tuple[ModelLocalReviewState, ModelLocalReviewPhaseEvent]:
        """Advance the FSM by one phase."""
        from_phase = state.current_phase

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        now = datetime.now(tz=UTC)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                to_phase = EnumLocalReviewPhase.FAILED
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

            event = ModelLocalReviewPhaseEvent(
                correlation_id=state.correlation_id,
                from_phase=from_phase,
                to_phase=to_phase,
                success=False,
                timestamp=now,
                error_message=error_message,
            )
            return new_state, event

        to_phase = next_phase(from_phase, is_clean=is_clean)

        updates: dict[str, object] = {
            "current_phase": to_phase,
            "consecutive_failures": 0,
            "error_message": None,
            "issues_found": state.issues_found + issues_found,
            "issues_fixed": state.issues_fixed + issues_fixed,
        }

        if is_clean and from_phase == EnumLocalReviewPhase.CHECK_CLEAN:
            updates["consecutive_clean_runs"] = state.consecutive_clean_runs + 1
        elif from_phase == EnumLocalReviewPhase.CHECK_CLEAN:
            updates["consecutive_clean_runs"] = 0

        if to_phase == EnumLocalReviewPhase.REVIEW:
            updates["iteration_count"] = state.iteration_count + 1

        new_state = state.model_copy(update=updates)

        event = ModelLocalReviewPhaseEvent(
            correlation_id=state.correlation_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=True,
            timestamp=now,
        )
        return new_state, event

    def make_completed_event(
        self,
        state: ModelLocalReviewState,
        started_at: datetime,
    ) -> ModelLocalReviewCompletedEvent:
        """Create a completion event from the final state."""
        return ModelLocalReviewCompletedEvent(
            correlation_id=state.correlation_id,
            final_phase=state.current_phase,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            iteration_count=state.iteration_count,
            issues_found=state.issues_found,
            issues_fixed=state.issues_fixed,
            error_message=state.error_message,
        )

    def serialize_event(self, event: ModelLocalReviewPhaseEvent) -> bytes:
        """Serialize a phase event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def serialize_completed(self, event: ModelLocalReviewCompletedEvent) -> bytes:
        """Serialize a completed event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def handle(
        self, command: ModelLocalReviewStartCommand
    ) -> ModelLocalReviewCompletedEvent:
        """Execute the local review pipeline."""
        _state, _events, completed = self.run_full_pipeline(command)
        return completed

    def run_full_pipeline(
        self,
        command: ModelLocalReviewStartCommand,
        check_clean_results: list[bool] | None = None,
    ) -> tuple[
        ModelLocalReviewState,
        list[ModelLocalReviewPhaseEvent],
        ModelLocalReviewCompletedEvent,
    ]:
        """Run a complete review loop with provided clean check results.

        check_clean_results is a list of booleans for each CHECK_CLEAN phase.
        True means clean, False means loop back. If not provided, first check
        is clean (single iteration).
        """
        started_at = datetime.now(tz=UTC)
        state = self.start(command)
        events: list[ModelLocalReviewPhaseEvent] = []
        clean_results = list(check_clean_results or [True])
        clean_idx = 0

        while state.current_phase not in TERMINAL_PHASES:
            if state.current_phase == EnumLocalReviewPhase.CHECK_CLEAN:
                is_clean = (
                    clean_results[clean_idx] if clean_idx < len(clean_results) else True
                )
                clean_idx += 1
                state, event = self.advance(
                    state, phase_success=True, is_clean=is_clean
                )
            else:
                state, event = self.advance(state, phase_success=True)
            events.append(event)

            if (
                state.iteration_count >= state.max_iterations
                and state.current_phase not in TERMINAL_PHASES
            ):
                state = state.model_copy(
                    update={
                        "current_phase": EnumLocalReviewPhase.DONE,
                        "error_message": "Max iterations reached",
                    }
                )
                break

        completed = self.make_completed_event(state, started_at)
        return state, events, completed


__all__: list[str] = ["HandlerLocalReview"]
