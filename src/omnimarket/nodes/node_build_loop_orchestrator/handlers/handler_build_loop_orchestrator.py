"""HandlerBuildLoopOrchestrator -- top-level orchestrator composing 6 sub-handlers.

The orchestrator REACTS to reducer-approved state. It never independently
decides phase transitions -- those are the sole authority of the FSM reducer
(HandlerBuildLoop from node_build_loop).

Flow per cycle:
    1. Receive start command -> initialize FSM state via HandlerBuildLoop
    2. Advance FSM through each phase, invoking the corresponding sub-handler
    3. Feed sub-handler results back to FSM via advance()
    4. Repeat until FSM reaches COMPLETE or FAILED
    5. Emit phase transition events via the injected event bus

Sub-handlers are injected via protocol-based DI:
    - ProtocolCloseoutHandler    (node_closeout_effect)
    - ProtocolVerifyHandler      (node_verify_effect)
    - ProtocolRsdFillHandler     (node_rsd_fill_compute)
    - ProtocolTicketClassifyHandler (node_ticket_classify_compute)
    - ProtocolBuildDispatchHandler  (node_build_dispatch_effect)

The 6th sub-handler is HandlerBuildLoop itself (the FSM reducer from
node_build_loop), which is used directly since it lives in this package.

Related:
    - OMN-7583: Migrate build loop orchestrator to omnimarket
    - OMN-7575: Build loop migration epic
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from omnimarket.nodes.node_build_loop.handlers.handler_build_loop import (
    HandlerBuildLoop,
)
from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
    ModelLoopStartCommand,
)
from omnimarket.nodes.node_build_loop.models.model_loop_state import (
    TERMINAL_PHASES,
    EnumBuildLoopPhase,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_loop_cycle_summary import (
    ModelLoopCycleSummary,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_orchestrator_result import (
    ModelOrchestratorResult,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
    ProtocolBuildDispatchHandler,
    ProtocolCloseoutHandler,
    ProtocolRsdFillHandler,
    ProtocolTicketClassifyHandler,
    ProtocolVerifyHandler,
    ScoredTicket,
)

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

logger = logging.getLogger(__name__)

# Topics declared in contract.yaml -- referenced here for event publishing
TOPIC_PHASE_TRANSITION = (
    "onex.evt.omnimarket.build-loop-orchestrator-phase-transition.v1"
)
TOPIC_COMPLETED = "onex.evt.omnimarket.build-loop-orchestrator-completed.v1"


class HandlerBuildLoopOrchestrator:
    """Top-level orchestrator composing 6 sub-handlers via FSM reducer.

    Takes protocol-based sub-handler dependencies and an optional event bus
    for publishing phase transition events. Synchronous sub-handler calls,
    in-process state ownership.
    """

    def __init__(
        self,
        *,
        closeout: ProtocolCloseoutHandler,
        verify: ProtocolVerifyHandler,
        rsd_fill: ProtocolRsdFillHandler,
        classify: ProtocolTicketClassifyHandler,
        dispatch: ProtocolBuildDispatchHandler,
        event_bus: ProtocolEventBusPublisher | None = None,
    ) -> None:
        self._fsm = HandlerBuildLoop()
        self._closeout = closeout
        self._verify = verify
        self._rsd_fill = rsd_fill
        self._classify = classify
        self._dispatch = dispatch
        self._event_bus = event_bus

        # Inter-phase state: carry results between fill -> classify -> build
        self._last_fill_result: tuple[ScoredTicket, ...] = ()
        self._last_classify_result: tuple[BuildTarget, ...] = ()

    async def handle(
        self,
        command: ModelLoopStartCommand,
    ) -> ModelOrchestratorResult:
        """Run the autonomous build loop for up to max_cycles.

        Args:
            command: Start command with configuration.

        Returns:
            ModelOrchestratorResult with per-cycle summaries.
        """
        logger.info(
            "[BUILD-LOOP-ORCH] === ENTRY === handle() called "
            "(correlation_id=%s, max_cycles=%d, dry_run=%s, skip_closeout=%s)",
            command.correlation_id,
            command.max_cycles,
            command.dry_run,
            command.skip_closeout,
        )

        summaries: list[ModelLoopCycleSummary] = []
        total_dispatched = 0
        cycles_completed = 0
        cycles_failed = 0

        for cycle_idx in range(command.max_cycles):
            logger.info(
                "[BUILD-LOOP-ORCH] Starting cycle %d/%d (correlation_id=%s)",
                cycle_idx + 1,
                command.max_cycles,
                command.correlation_id,
            )
            summary = await self._run_cycle(command)
            summaries.append(summary)

            if summary.final_phase == EnumBuildLoopPhase.COMPLETE:
                cycles_completed += 1
                total_dispatched += summary.tickets_dispatched
            else:
                cycles_failed += 1
                logger.warning(
                    "[BUILD-LOOP-ORCH] Cycle %d failed in phase %s: %s",
                    cycle_idx + 1,
                    summary.final_phase.value,
                    summary.error_message,
                )
                break

        logger.info(
            "[BUILD-LOOP-ORCH] === EXIT === %d completed, %d failed, "
            "%d dispatched (correlation_id=%s)",
            cycles_completed,
            cycles_failed,
            total_dispatched,
            command.correlation_id,
        )

        return ModelOrchestratorResult(
            correlation_id=command.correlation_id,
            cycles_completed=cycles_completed,
            cycles_failed=cycles_failed,
            cycle_summaries=tuple(summaries),
            total_tickets_dispatched=total_dispatched,
        )

    async def _run_cycle(
        self,
        command: ModelLoopStartCommand,
    ) -> ModelLoopCycleSummary:
        """Run a single build loop cycle through the FSM."""
        cycle_start = datetime.now(tz=UTC)
        correlation_id = command.correlation_id

        # Reset inter-phase state
        self._last_fill_result = ()
        self._last_classify_result = ()

        # Initialize FSM state via the reducer
        state = self._fsm.start(command)

        # Advance FSM through IDLE to first active phase
        state, event = self._fsm.advance(state, phase_success=True)
        await self._publish_phase_event(event)

        # Process phases until terminal state
        while state.current_phase not in TERMINAL_PHASES:
            success, error_msg, metrics = await self._execute_phase(
                state.current_phase,
                correlation_id=correlation_id,
                dry_run=command.dry_run,
                max_tickets=command.max_tickets,
            )

            state, event = self._fsm.advance(
                state,
                phase_success=success,
                error_message=error_msg,
                tickets_filled=metrics.get("tickets_filled", 0),
                tickets_classified=metrics.get("tickets_classified", 0),
                tickets_dispatched=metrics.get("tickets_dispatched", 0),
            )
            await self._publish_phase_event(event)

        return ModelLoopCycleSummary(
            correlation_id=correlation_id,
            cycle_number=max(state.cycle_count, 1),
            final_phase=state.current_phase,
            started_at=cycle_start,
            completed_at=datetime.now(tz=UTC),
            tickets_filled=state.tickets_filled,
            tickets_classified=state.tickets_classified,
            tickets_dispatched=state.tickets_dispatched,
            error_message=state.error_message,
        )

    async def _execute_phase(
        self,
        phase: EnumBuildLoopPhase,
        *,
        correlation_id: UUID,
        dry_run: bool,
        max_tickets: int = 5,
    ) -> tuple[bool, str | None, dict[str, int]]:
        """Execute the sub-handler for the given phase.

        Returns (success, error_message, metrics_dict).
        """
        metrics: dict[str, int] = {}

        try:
            if phase == EnumBuildLoopPhase.CLOSING_OUT:
                await self._closeout.handle(
                    correlation_id=correlation_id,
                    dry_run=dry_run,
                )
                return True, None, metrics

            if phase == EnumBuildLoopPhase.VERIFYING:
                result = await self._verify.handle(
                    correlation_id=correlation_id,
                    dry_run=dry_run,
                )
                if not result.all_critical_passed:
                    return False, "Critical verification checks failed", metrics
                return True, None, metrics

            if phase == EnumBuildLoopPhase.FILLING:
                fill_result = await self._rsd_fill.handle(
                    correlation_id=correlation_id,
                    scored_tickets=(),
                    max_tickets=max_tickets,
                )
                self._last_fill_result = fill_result.selected_tickets
                metrics["tickets_filled"] = fill_result.total_selected
                return True, None, metrics

            if phase == EnumBuildLoopPhase.CLASSIFYING:
                classify_result = await self._classify.handle(
                    correlation_id=correlation_id,
                    tickets=self._last_fill_result,
                )
                self._last_classify_result = tuple(
                    BuildTarget(
                        ticket_id=c.ticket_id,
                        title=c.title,
                        buildability=c.buildability,
                    )
                    for c in classify_result.classifications
                    if c.buildability == "auto_buildable"
                )
                metrics["tickets_classified"] = len(
                    classify_result.classifications,
                )
                return True, None, metrics

            if phase == EnumBuildLoopPhase.BUILDING:
                dispatch_result = await self._dispatch.handle(
                    correlation_id=correlation_id,
                    targets=self._last_classify_result,
                    dry_run=dry_run,
                )

                # Publish delegation payloads via event bus
                if self._event_bus is not None:
                    for dp in dispatch_result.delegation_payloads:
                        await self._event_bus.publish(
                            topic=dp.topic,
                            key=None,
                            value=json.dumps(dp.payload, default=str).encode(),
                        )

                metrics["tickets_dispatched"] = dispatch_result.total_dispatched
                return True, None, metrics

            return False, f"Unknown phase: {phase}", metrics

        except Exception as exc:
            logger.exception(
                "[BUILD-LOOP-ORCH] Phase %s failed: %s (correlation_id=%s)",
                phase.value,
                exc,
                correlation_id,
            )
            return False, str(exc), metrics

    async def _publish_phase_event(self, event: object) -> None:
        """Publish a phase transition event to the event bus."""
        if self._event_bus is None:
            return

        from omnimarket.nodes.node_build_loop.models.model_phase_transition_event import (
            ModelPhaseTransitionEvent,
        )

        if isinstance(event, ModelPhaseTransitionEvent):
            payload = json.dumps(event.model_dump(mode="json")).encode()
            await self._event_bus.publish(
                topic=TOPIC_PHASE_TRANSITION,
                key=None,
                value=payload,
            )


__all__: list[str] = ["HandlerBuildLoopOrchestrator"]
