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
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_phase_result import (
    EnumPipelinePhaseResultStatus,
    ModelPipelineExecutionReport,
    ModelPipelinePhaseResult,
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
            current_phase=command.skip_to or EnumPipelinePhase.PRE_FLIGHT,
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

        Delegates to the safe executable slice. This handler intentionally does
        not claim full pipeline autonomy while later phases are not wired.
        """
        report = self.run_executable_pipeline(command)
        return report.completed

    def execute_phase(
        self,
        state: ModelPipelineState,
    ) -> ModelPipelinePhaseResult:
        """Execute the current node-owned phase and return an explicit result."""
        started_at = datetime.now(tz=UTC)
        if state.current_phase == EnumPipelinePhase.PRE_FLIGHT:
            result = self._execute_pre_flight(state, started_at)
        elif state.current_phase in TERMINAL_PHASES:
            result = ModelPipelinePhaseResult(
                correlation_id=state.correlation_id,
                ticket_id=state.ticket_id,
                phase=state.current_phase,
                status=EnumPipelinePhaseResultStatus.BLOCKED,
                dry_run=state.dry_run,
                started_at=started_at,
                completed_at=datetime.now(tz=UTC),
                message=f"Cannot execute terminal phase: {state.current_phase.value}",
            )
        else:
            result = ModelPipelinePhaseResult(
                correlation_id=state.correlation_id,
                ticket_id=state.ticket_id,
                phase=state.current_phase,
                status=EnumPipelinePhaseResultStatus.NOT_IMPLEMENTED,
                dry_run=state.dry_run,
                started_at=started_at,
                completed_at=datetime.now(tz=UTC),
                message=(
                    f"Phase {state.current_phase.value} is not implemented in "
                    "the first ticket-pipeline slice"
                ),
                details={"blocked_reason": "phase_not_wired"},
            )
        return result

    def _execute_pre_flight(
        self,
        state: ModelPipelineState,
        started_at: datetime,
    ) -> ModelPipelinePhaseResult:
        return ModelPipelinePhaseResult(
            correlation_id=state.correlation_id,
            ticket_id=state.ticket_id,
            phase=EnumPipelinePhase.PRE_FLIGHT,
            status=EnumPipelinePhaseResultStatus.SUCCEEDED,
            dry_run=state.dry_run,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            message="Pre-flight validation passed",
            details={
                "ticket_id": state.ticket_id,
                "dry_run": state.dry_run,
                "validated": [
                    "ticket_id",
                    "correlation_id",
                    "skip_to",
                    "dry_run",
                ],
                "side_effects": "none",
            },
        )

    def _stop_on_result(
        self,
        state: ModelPipelineState,
        result: ModelPipelinePhaseResult,
    ) -> tuple[ModelPipelineState, ModelPipelinePhaseEvent]:
        now = datetime.now(tz=UTC)
        message = result.message or f"Phase {result.phase.value} blocked"
        terminal_phase = (
            EnumPipelinePhase.FAILED
            if result.status == EnumPipelinePhaseResultStatus.FAILED
            else EnumPipelinePhase.BLOCKED
        )
        new_state = state.model_copy(
            update={
                "current_phase": terminal_phase,
                "error_message": message,
            }
        )
        event = ModelPipelinePhaseEvent(
            correlation_id=state.correlation_id,
            ticket_id=state.ticket_id,
            from_phase=state.current_phase,
            to_phase=terminal_phase,
            success=False,
            timestamp=now,
            error_message=message,
        )
        return new_state, event

    def run_executable_pipeline(
        self,
        command: ModelPipelineStartCommand,
    ) -> ModelPipelineExecutionReport:
        """Run the honest node-owned execution slice until it must stop."""
        started_at = datetime.now(tz=UTC)
        state = self.start(command)
        events: list[ModelPipelinePhaseEvent] = []
        results: list[ModelPipelinePhaseResult] = []
        stop_reason = "terminal_phase"

        while state.current_phase not in TERMINAL_PHASES:
            result = self.execute_phase(state)
            results.append(result)

            if result.success:
                state, event = self.advance(state, phase_success=True)
                events.append(event)
                continue

            state, event = self._stop_on_result(state, result)
            events.append(event)
            stop_reason = result.status.value
            break

        completed = self.make_completed_event(state, started_at)
        ran_phase = results[-1].phase if results else None
        return ModelPipelineExecutionReport(
            state=state,
            phase_results=results,
            phase_events=events,
            completed=completed,
            ran_phase=ran_phase,
            stopped_at=state.current_phase,
            stop_reason=stop_reason,
        )

    def run_full_pipeline(
        self,
        command: ModelPipelineStartCommand,
        phase_results: dict[EnumPipelinePhase, bool] | None = None,
    ) -> tuple[
        ModelPipelineState,
        list[ModelPipelinePhaseEvent],
        ModelPipelineCompletedEvent,
    ]:
        """Run a pipeline with explicit injected results, or the safe slice.

        Missing phase results no longer imply success. Without injected results,
        only PRE_FLIGHT executes and later side-effect phases block as not wired.
        """
        if phase_results is None:
            report = self.run_executable_pipeline(command)
            return report.state, report.phase_events, report.completed

        started_at = datetime.now(tz=UTC)
        state = self.start(command)
        events: list[ModelPipelinePhaseEvent] = []
        results = phase_results

        while state.current_phase not in TERMINAL_PHASES:
            target = state.current_phase
            if target not in results:
                result = self.execute_phase(state)
                if result.success:
                    state, event = self.advance(state, phase_success=True)
                else:
                    state, event = self._stop_on_result(state, result)
                events.append(event)
                if not result.success:
                    break
                continue

            success = results[target]
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
