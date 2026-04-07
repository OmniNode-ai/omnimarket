"""HandlerPrPolish — FSM state machine for PR readiness polish.

Pure state machine logic. Phases: INIT -> RESOLVE_CONFLICTS -> FIX_CI ->
ADDRESS_COMMENTS -> LOCAL_REVIEW -> DONE.

Circuit breaker: 3 consecutive failures -> FAILED.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_pr_polish.models.model_pr_polish_completed_event import (
    ModelPrPolishCompletedEvent,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_phase_event import (
    ModelPrPolishPhaseEvent,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command import (
    ModelPrPolishStartCommand,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_state import (
    TERMINAL_PHASES,
    EnumPrPolishPhase,
    ModelPrPolishState,
    next_phase,
)

logger = logging.getLogger(__name__)


class HandlerPrPolish:
    """FSM handler for PR polish.

    Pure logic — no external I/O. Callers wire event bus publish/subscribe.
    """

    def start(self, command: ModelPrPolishStartCommand) -> ModelPrPolishState:
        """Initialize polish state from a start command."""
        return ModelPrPolishState(
            correlation_id=command.correlation_id,
            pr_number=command.pr_number,
            current_phase=EnumPrPolishPhase.INIT,
            skip_conflicts=command.skip_conflicts,
            dry_run=command.dry_run,
            max_consecutive_failures=3,
        )

    def advance(
        self,
        state: ModelPrPolishState,
        phase_success: bool,
        error_message: str | None = None,
        conflicts_resolved: int = 0,
        ci_fixes_applied: int = 0,
        comments_addressed: int = 0,
    ) -> tuple[ModelPrPolishState, ModelPrPolishPhaseEvent]:
        """Advance the FSM by one phase."""
        from_phase = state.current_phase

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        now = datetime.now(tz=UTC)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                to_phase = EnumPrPolishPhase.FAILED
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

            event = ModelPrPolishPhaseEvent(
                correlation_id=state.correlation_id,
                from_phase=from_phase,
                to_phase=to_phase,
                success=False,
                timestamp=now,
                error_message=error_message,
            )
            return new_state, event

        to_phase = next_phase(from_phase, skip_conflicts=state.skip_conflicts)
        new_state = state.model_copy(
            update={
                "current_phase": to_phase,
                "consecutive_failures": 0,
                "error_message": None,
                "conflicts_resolved": state.conflicts_resolved + conflicts_resolved,
                "ci_fixes_applied": state.ci_fixes_applied + ci_fixes_applied,
                "comments_addressed": state.comments_addressed + comments_addressed,
            }
        )

        event = ModelPrPolishPhaseEvent(
            correlation_id=state.correlation_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=True,
            timestamp=now,
        )
        return new_state, event

    def make_completed_event(
        self,
        state: ModelPrPolishState,
        started_at: datetime,
    ) -> ModelPrPolishCompletedEvent:
        """Create a completion event from the final state."""
        return ModelPrPolishCompletedEvent(
            correlation_id=state.correlation_id,
            final_phase=state.current_phase,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            pr_number=state.pr_number,
            conflicts_resolved=state.conflicts_resolved,
            ci_fixes_applied=state.ci_fixes_applied,
            comments_addressed=state.comments_addressed,
            error_message=state.error_message,
        )

    def serialize_event(self, event: ModelPrPolishPhaseEvent) -> bytes:
        """Serialize a phase event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def serialize_completed(self, event: ModelPrPolishCompletedEvent) -> bytes:
        """Serialize a completed event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to run_full_pipeline with a ModelPrPolishStartCommand
        constructed from input_data.
        """
        command = ModelPrPolishStartCommand(**input_data)
        _state, _events, completed = self.run_full_pipeline(command)
        return completed.model_dump(mode="json")

    def run_full_pipeline(
        self,
        command: ModelPrPolishStartCommand,
        phase_results: dict[EnumPrPolishPhase, bool] | None = None,
    ) -> tuple[
        ModelPrPolishState,
        list[ModelPrPolishPhaseEvent],
        ModelPrPolishCompletedEvent,
    ]:
        """Run a complete polish pipeline with provided results."""
        started_at = datetime.now(tz=UTC)
        state = self.start(command)
        events: list[ModelPrPolishPhaseEvent] = []
        results = phase_results or {}

        while state.current_phase not in TERMINAL_PHASES:
            target = next_phase(
                state.current_phase, skip_conflicts=state.skip_conflicts
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


__all__: list[str] = ["HandlerPrPolish"]
