"""HandlerHostileReviewer — FSM state machine for multi-model adversarial review.

Pure state machine logic. Phases: INIT -> DISPATCH_REVIEWS -> AGGREGATE ->
CONVERGENCE_CHECK -> REPORT -> DONE.

Circuit breaker: 3 consecutive failures -> FAILED.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_completed_event import (
    ModelHostileReviewerCompletedEvent,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_phase_event import (
    ModelHostileReviewerPhaseEvent,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_state import (
    TERMINAL_PHASES,
    EnumHostileReviewerPhase,
    ModelHostileReviewerState,
    next_phase,
)

logger = logging.getLogger(__name__)


class HandlerHostileReviewer:
    """FSM handler for the hostile reviewer.

    Pure logic — no external I/O. Callers wire event bus publish/subscribe.
    """

    def start(
        self, command: ModelHostileReviewerStartCommand
    ) -> ModelHostileReviewerState:
        """Initialize reviewer state from a start command."""
        return ModelHostileReviewerState(
            correlation_id=command.correlation_id,
            current_phase=EnumHostileReviewerPhase.INIT,
            dry_run=command.dry_run,
            max_consecutive_failures=3,
        )

    def advance(
        self,
        state: ModelHostileReviewerState,
        phase_success: bool,
        error_message: str | None = None,
        findings: int = 0,
        is_clean_pass: bool = False,
    ) -> tuple[ModelHostileReviewerState, ModelHostileReviewerPhaseEvent]:
        """Advance the FSM by one phase."""
        from_phase = state.current_phase

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        now = datetime.now(tz=UTC)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                to_phase = EnumHostileReviewerPhase.FAILED
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

            event = ModelHostileReviewerPhaseEvent(
                correlation_id=state.correlation_id,
                from_phase=from_phase,
                to_phase=to_phase,
                success=False,
                timestamp=now,
                error_message=error_message,
            )
            return new_state, event

        to_phase = next_phase(from_phase)

        updates: dict[str, object] = {
            "current_phase": to_phase,
            "consecutive_failures": 0,
            "error_message": None,
            "total_findings": state.total_findings + findings,
        }
        if is_clean_pass:
            updates["consecutive_clean"] = state.consecutive_clean + 1
        else:
            updates["consecutive_clean"] = 0

        if to_phase == EnumHostileReviewerPhase.DONE:
            updates["pass_count"] = state.pass_count + 1

        new_state = state.model_copy(update=updates)

        event = ModelHostileReviewerPhaseEvent(
            correlation_id=state.correlation_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=True,
            timestamp=now,
        )
        return new_state, event

    def make_completed_event(
        self,
        state: ModelHostileReviewerState,
        started_at: datetime,
    ) -> ModelHostileReviewerCompletedEvent:
        """Create a completion event from the final state."""
        return ModelHostileReviewerCompletedEvent(
            correlation_id=state.correlation_id,
            final_phase=state.current_phase,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            pass_count=state.pass_count,
            total_findings=state.total_findings,
            error_message=state.error_message,
        )

    def serialize_event(self, event: ModelHostileReviewerPhaseEvent) -> bytes:
        """Serialize a phase event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def serialize_completed(self, event: ModelHostileReviewerCompletedEvent) -> bytes:
        """Serialize a completed event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def handle(
        self, command: ModelHostileReviewerStartCommand
    ) -> ModelHostileReviewerCompletedEvent:
        """Execute the hostile reviewer pipeline."""
        _state, _events, completed = self.run_full_pipeline(command)
        return completed

    def run_full_pipeline(
        self,
        command: ModelHostileReviewerStartCommand,
        phase_results: dict[EnumHostileReviewerPhase, bool] | None = None,
    ) -> tuple[
        ModelHostileReviewerState,
        list[ModelHostileReviewerPhaseEvent],
        ModelHostileReviewerCompletedEvent,
    ]:
        """Run a complete review pipeline with provided results."""
        started_at = datetime.now(tz=UTC)
        state = self.start(command)
        events: list[ModelHostileReviewerPhaseEvent] = []
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


__all__: list[str] = ["HandlerHostileReviewer"]
