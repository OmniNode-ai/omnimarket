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
    - OMN-8165: Wire overseer verifier into build loop (Phase 1 advisory seam)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.protocols.event_bus.protocol_event_envelope import (
    ProtocolEventEnvelope,
)

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
from omnimarket.nodes.node_build_loop_orchestrator.topics import (
    TOPIC_DOD_CHECKED,
    TOPIC_OVERSEER_VERIFICATION_COMPLETED,
    TOPIC_OVERSEER_VERIFY_REQUESTED,
)
from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier import (
    HandlerOverseerVerifier,
)
from omnimarket.nodes.node_overseer_verifier.models.model_verifier_request import (
    ModelVerifierRequest,
)
from omnimarket.protocols.protocol_overseer_verifier import ProtocolOverseerVerifier

if TYPE_CHECKING:
    from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

logger = logging.getLogger(__name__)

_VERIFIER_TIMEOUT_SECONDS = 120


def _load_contract(contract_path: Path | None = None) -> dict[str, Any]:
    """Load the node's contract.yaml."""
    _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
    with open(_path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


class HandlerBuildLoopOrchestrator:
    """Top-level orchestrator composing 6 sub-handlers via FSM reducer.

    Takes protocol-based sub-handler dependencies and an optional event bus
    for publishing phase transition events. Synchronous sub-handler calls,
    in-process state ownership.

    All sub-handler arguments are optional to support zero-arg construction by
    the auto-wiring runtime (``onex run``). When omitted, concrete default
    implementations are lazy-initialized on the first call to ``handle()``.
    Callers that want explicit DI can still pass all five dependencies.
    """

    def __init__(
        self,
        *,
        closeout: ProtocolCloseoutHandler | None = None,
        verify: ProtocolVerifyHandler | None = None,
        rsd_fill: ProtocolRsdFillHandler | None = None,
        classify: ProtocolTicketClassifyHandler | None = None,
        dispatch: ProtocolBuildDispatchHandler | None = None,
        event_bus: ProtocolEventBusPublisher | None = None,
        contract_path: Path | None = None,
        overseer_verifier: ProtocolOverseerVerifier | None = None,
    ) -> None:
        contract = _load_contract(contract_path)
        publish_topics: list[str] = contract.get("event_bus", {}).get(
            "publish_topics", []
        )

        self._topic_phase_transition = next(
            (t for t in publish_topics if "phase-transition" in t), ""
        )
        self._topic_completed = next(
            (t for t in publish_topics if "completed" in t), ""
        )

        self._fsm = HandlerBuildLoop()
        self._closeout = closeout
        self._verify = verify
        self._verify_explicitly_injected: bool = verify is not None
        self._rsd_fill = rsd_fill
        self._classify = classify
        self._dispatch = dispatch
        self._event_bus = event_bus
        self._overseer_verifier = HandlerOverseerVerifier()
        # Advisory overseer seam (Phase 1 — OMN-8165). Injected via DI for testing;
        # defaults to None (advisory check uses self._overseer_verifier directly).
        self._advisory_overseer: ProtocolOverseerVerifier | None = overseer_verifier

        # Inter-phase state: carry results between fill -> classify -> build
        self._last_fill_result: tuple[ScoredTicket, ...] = ()
        self._last_classify_result: tuple[BuildTarget, ...] = ()

    def _ensure_sub_handlers(self) -> None:
        """Lazy-initialize sub-handlers from default implementations if not injected."""
        if self._closeout is None:
            from omnimarket.nodes.node_closeout_effect.handlers.handler_closeout import (
                HandlerCloseout,
            )

            self._closeout = cast(ProtocolCloseoutHandler, HandlerCloseout())
        if self._verify is None:
            from omnimarket.nodes.node_verify_effect.handlers.handler_verify import (
                HandlerVerify,
            )

            self._verify = cast(ProtocolVerifyHandler, HandlerVerify())
        if self._rsd_fill is None:
            from omnimarket.nodes.node_rsd_fill_compute.handlers.handler_rsd_fill import (
                HandlerRsdFill,
            )

            self._rsd_fill = cast(ProtocolRsdFillHandler, HandlerRsdFill())
        if self._classify is None:
            from omnimarket.nodes.node_ticket_classify_compute.handlers.handler_ticket_classify import (
                HandlerTicketClassify,
            )

            self._classify = cast(
                ProtocolTicketClassifyHandler, HandlerTicketClassify()
            )
        if self._dispatch is None:
            from omnimarket.nodes.node_build_dispatch_effect.handlers.handler_build_dispatch import (
                HandlerBuildDispatch,
            )

            self._dispatch = cast(ProtocolBuildDispatchHandler, HandlerBuildDispatch())

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
        self._ensure_sub_handlers()
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

        # _ensure_sub_handlers() is called before _run_cycle; assert for mypy
        assert self._closeout is not None
        assert self._verify is not None
        assert self._rsd_fill is not None
        assert self._classify is not None
        assert self._dispatch is not None

        try:
            if phase == EnumBuildLoopPhase.CLOSING_OUT:
                await self._closeout.handle(
                    correlation_id=correlation_id,
                    dry_run=dry_run,
                )
                return True, None, metrics

            if phase == EnumBuildLoopPhase.VERIFYING:
                return await self._run_overseer_verify(
                    correlation_id=correlation_id,
                    dry_run=dry_run,
                )

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

                # Phase 1 advisory overseer seam (OMN-8165): verify classify output
                # before advancing to BUILDING. Advisory only — ESCALATE is logged
                # but does not block. Hard gate comes in Phase 2.
                self._run_advisory_overseer_check(
                    correlation_id=correlation_id,
                    classified_count=len(self._last_classify_result),
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
                        envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
                            payload=dp.payload,
                            correlation_id=str(
                                dp.payload.get("correlation_id") or correlation_id
                            ),
                            event_type="delegation-requested",
                            source_tool="HandlerBuildLoopOrchestrator",
                        )
                        await self._event_bus.publish_envelope(
                            envelope=cast(ProtocolEventEnvelope[object], envelope),
                            topic=dp.topic,
                        )

                metrics["tickets_dispatched"] = dispatch_result.total_dispatched

                # Run DoD verification for each dispatched target before advancing FSM
                verification_failures: list[str] = []
                for target in self._last_classify_result:
                    verifier_result = self._overseer_verifier.verify(
                        ModelVerifierRequest(
                            task_id=target.ticket_id,
                            status="completed",
                            domain="build_loop",
                            node_id=str(correlation_id),
                        )
                    )
                    verdict = str(verifier_result.get("verdict", "FAIL"))
                    raw_checks = verifier_result.get("checks", [])
                    checks: list[object] = (
                        list(raw_checks) if isinstance(raw_checks, list) else []
                    )
                    await self._publish_dod_event(
                        task_id=target.ticket_id,
                        verdict=verdict,
                        checks=checks,
                        correlation_id=correlation_id,
                    )
                    if verdict != "PASS":
                        failure_reason = str(
                            verifier_result.get("failure_class", "UNKNOWN")
                        )
                        verification_failures.append(
                            f"{target.ticket_id}: {failure_reason}"
                        )
                        logger.warning(
                            "[BUILD-LOOP-ORCH] DoD verification FAIL for %s: %s "
                            "(correlation_id=%s)",
                            target.ticket_id,
                            failure_reason,
                            correlation_id,
                        )

                if verification_failures:
                    return (
                        False,
                        f"DoD verification failed for: {', '.join(verification_failures)}",
                        metrics,
                    )

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

    def _run_advisory_overseer_check(
        self,
        *,
        correlation_id: UUID,
        classified_count: int,
    ) -> None:
        """Advisory overseer check after CLASSIFYING phase (Phase 1 — OMN-8165).

        Runs the deterministic 5-check gate against the CLASSIFYING output.
        ESCALATE verdict is logged but does NOT block phase progression.
        This is a soft gate — hard gating is deferred to Phase 2.

        Args:
            correlation_id: Cycle correlation identifier.
            classified_count: Number of tickets classified as auto_buildable.
        """
        verifier_result = self._overseer_verifier.verify(
            ModelVerifierRequest(
                task_id=str(correlation_id),
                status="classifying_complete",
                domain="build_loop",
                node_id="node_build_loop_orchestrator",
                payload={"classified_count": classified_count},
            )
        )
        verdict = str(verifier_result.get("verdict", "PASS"))
        summary = str(verifier_result.get("summary", ""))

        if verdict == "ESCALATE":
            logger.error(
                "[BUILD-LOOP-ORCH] Overseer ESCALATE after CLASSIFYING "
                "(advisory — not blocking): %s (correlation_id=%s)",
                summary,
                correlation_id,
            )
        elif verdict == "FAIL":
            logger.warning(
                "[BUILD-LOOP-ORCH] Overseer FAIL after CLASSIFYING "
                "(advisory — not blocking): %s (correlation_id=%s)",
                summary,
                correlation_id,
            )
        else:
            logger.debug(
                "[BUILD-LOOP-ORCH] Overseer PASS after CLASSIFYING (correlation_id=%s)",
                correlation_id,
            )

    async def _run_overseer_verify(
        self,
        correlation_id: UUID,
        dry_run: bool,
    ) -> tuple[bool, str | None, dict[str, int]]:
        """VERIFYING phase: publish verify command, await correlated verdict.

        1. Publish onex.cmd.overseer.verify-requested.v1 with correlation_id
        2. Await onex.evt.overseer.verification-completed.v1 filtered by correlation_id
        3. Timeout after _VERIFIER_TIMEOUT_SECONDS → FAILED
        4. passed=False → FAILED with failed_criteria surfaced
        5. passed=True → advance to FILLING

        If event_bus is None, dry_run=True, or a verify handler was explicitly injected
        at construction time, fall back to the legacy verify handler. The overseer event
        path is only taken when event_bus is present AND no legacy verify handler was
        injected (i.e. the auto-wired default is in use).
        """
        if self._event_bus is None or self._verify_explicitly_injected or dry_run:
            # No event bus: fall back to legacy verify handler for standalone/dry-run
            assert self._verify is not None
            result = await self._verify.handle(
                correlation_id=correlation_id,
                dry_run=dry_run,
            )
            if not result.all_critical_passed:
                return False, "Critical verification checks failed", {}
            return True, None, {}

        # 1. Publish verify command
        verify_cmd = json.dumps(
            {
                "correlation_id": str(correlation_id),
                "requested_by": "build_loop_orchestrator",
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }
        ).encode()
        await self._event_bus.publish(
            topic=TOPIC_OVERSEER_VERIFY_REQUESTED,
            key=None,
            value=verify_cmd,
        )
        logger.info(
            "[BUILD-LOOP-ORCH] Published overseer verify command (correlation_id=%s)",
            correlation_id,
        )

        # 2. Await correlated verdict via asyncio.Event / subscription
        verdict_event: asyncio.Event = asyncio.Event()
        verdict_payload: dict[str, Any] = {}

        async def on_verification_completed(msg: ModelEventMessage) -> None:
            try:
                data = json.loads(msg.value)
            except (json.JSONDecodeError, ValueError):
                return
            if str(data.get("correlation_id")) == str(correlation_id):
                verdict_payload.update(data)
                verdict_event.set()

        # Register callback on event bus if it supports subscribe; otherwise poll
        if hasattr(self._event_bus, "subscribe"):
            await self._event_bus.subscribe(
                TOPIC_OVERSEER_VERIFICATION_COMPLETED,
                on_message=on_verification_completed,
                group_id=f"build-loop-overseer-wait-{correlation_id}",
            )

        try:
            await asyncio.wait_for(
                verdict_event.wait(), timeout=_VERIFIER_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.warning(
                "[BUILD-LOOP-ORCH] Overseer verifier timed out after %ds "
                "(correlation_id=%s)",
                _VERIFIER_TIMEOUT_SECONDS,
                correlation_id,
            )
            return False, "verifier_timeout", {}

        # 3. Evaluate verdict
        passed = bool(verdict_payload.get("passed", False))
        if not passed:
            failed_criteria = verdict_payload.get("failed_criteria", [])
            reason = (
                "; ".join(str(c) for c in failed_criteria)
                if failed_criteria
                else "overseer verification failed"
            )
            logger.warning(
                "[BUILD-LOOP-ORCH] Overseer verifier FAILED: %s (correlation_id=%s)",
                reason,
                correlation_id,
            )
            return False, reason, {}

        logger.info(
            "[BUILD-LOOP-ORCH] Overseer verifier PASSED (correlation_id=%s)",
            correlation_id,
        )
        return True, None, {}

    async def _publish_dod_event(
        self,
        task_id: str,
        verdict: str,
        checks: list[object],
        correlation_id: UUID,
    ) -> None:
        """Publish a DoD verification event to the event bus."""
        if self._event_bus is None:
            return
        checks_passed = sum(
            1 for c in checks if isinstance(c, dict) and c.get("passed")
        )
        checks_failed = sum(
            1 for c in checks if isinstance(c, dict) and not c.get("passed")
        )
        payload = json.dumps(
            {
                "task_id": task_id,
                "verdict": verdict,
                "checks_passed": checks_passed,
                "checks_failed": checks_failed,
                "correlation_id": str(correlation_id),
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }
        ).encode()
        await self._event_bus.publish(
            topic=TOPIC_DOD_CHECKED,
            key=None,
            value=payload,
        )

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
                topic=self._topic_phase_transition,
                key=None,
                value=payload,
            )


__all__: list[str] = ["HandlerBuildLoopOrchestrator"]
