# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSwarmDispatchOrchestrator — FSM coordinator for swarm dispatch.

Drives the 7-state FSM: RECEIVED → HEALTH_CHECKED → DECOMPOSED →
ENDPOINTS_SELECTED → DISPATCHING → AGGREGATING → COMPLETED (or FAILED).

Pure coordination only. No HTTP calls, no LLM calls, no file I/O.
All I/O is delegated to effect nodes via command publication.

Design note: all FSM transition methods are synchronous and return the next
state plus a list of pending publish payloads. `handle_async` drives the full
chain with the real event bus; `handle` drives it in test mode (no publishes).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.enums import (
    EnumAggregationMode,
    EnumSubtaskStatus,
    EnumSwarmOrchestratorState,
    EnumSwarmRunStatus,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_orchestrator_state import (
    ModelEndpointHealth,
    ModelOrchestratorState,
    ModelSubtask,
    ModelSubtaskDispatch,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_swarm_dispatch_request import (
    ModelSwarmAggregateCommand,
    ModelSwarmConfig,
    ModelSwarmDecomposeCommand,
    ModelSwarmDispatchRequest,
    ModelSwarmFanoutCommand,
    ModelSwarmHealthCheckCommand,
    ModelSwarmSelectEndpointsCommand,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_swarm_dispatch_result import (
    ModelSwarmDispatchResult,
)

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

logger = logging.getLogger(__name__)

_FAILED_STATUSES = frozenset(
    {
        EnumSubtaskStatus.FAILED,
        EnumSubtaskStatus.TIMEOUT,
        EnumSubtaskStatus.CONTEXT_WINDOW_EXCEEDED,
    }
)

# (topic, payload) pairs queued by transition methods
_PendingPublish = tuple[str, dict[str, Any]]


def _load_contract(contract_path: Path | None = None) -> dict[str, Any]:
    path = contract_path or Path(__file__).parent.parent / "contract.yaml"
    with open(path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class InvalidFSMTransitionError(Exception):
    pass


class HandlerSwarmDispatchOrchestrator:
    """FSM orchestrator for swarm dispatch.

    All transition_* methods are synchronous and return (new_state, publishes).
    The `publishes` list contains (topic, payload) pairs to emit after the
    transition. This keeps the FSM logic testable without an event bus.

    `handle` runs the FSM with event_bus=None (no publishes).
    `handle_async` runs the FSM and awaits each publish on the real bus.
    """

    def __init__(
        self,
        *,
        event_bus: ProtocolEventBusPublisher | None = None,
        contract_path: Path | None = None,
    ) -> None:
        contract = _load_contract(contract_path)
        pub: list[str] = contract.get("event_bus", {}).get("publish_topics", [])
        self._topic_health_cmd = next(
            (t for t in pub if "swarm-check-endpoint-health" in t), ""
        )
        self._topic_decompose_cmd = next((t for t in pub if "swarm-decompose" in t), "")
        self._topic_select_cmd = next(
            (t for t in pub if "swarm-select-endpoints" in t), ""
        )
        self._topic_fanout_cmd = next((t for t in pub if "swarm-fanout" in t), "")
        self._topic_aggregate_cmd = next((t for t in pub if "swarm-aggregate" in t), "")
        self._topic_completed = next(
            (t for t in pub if "swarm-dispatch-completed" in t), ""
        )
        self._topic_failed = next((t for t in pub if "swarm-dispatch-failed" in t), "")
        self._event_bus = event_bus

    # ------------------------------------------------------------------
    # Sync entry point (no bus; used in tests and standalone mode)
    # ------------------------------------------------------------------

    def handle(self, request: ModelSwarmDispatchRequest) -> ModelSwarmDispatchResult:
        """Drive the full FSM without publishing (event_bus ignored)."""
        logger.info(
            "[SWARM-DISPATCH] === ENTRY === run_id=%s correlation_id=%s task=%r",
            request.run_id,
            request.correlation_id,
            request.task[:80],
        )
        state = ModelOrchestratorState(
            fsm_state=EnumSwarmOrchestratorState.RECEIVED,
            run_id=request.run_id,
            correlation_id=request.correlation_id,
            original_task=request.task,
        )
        try:
            state, _ = self.transition_received(state, request)
            return self._build_result(state, request)
        except Exception as exc:
            logger.error(
                "[SWARM-DISPATCH] FAILED run_id=%s error=%s", request.run_id, exc
            )
            failed_state = state.with_error(str(exc))
            return self._build_result(failed_state, request)

    # ------------------------------------------------------------------
    # Async entry point (uses real event bus)
    # ------------------------------------------------------------------

    async def handle_async(
        self, request: ModelSwarmDispatchRequest
    ) -> ModelSwarmDispatchResult:
        """Drive the FSM entry point and emit the initial health-check command."""
        logger.info(
            "[SWARM-DISPATCH] === ASYNC ENTRY === run_id=%s correlation_id=%s",
            request.run_id,
            request.correlation_id,
        )
        state = ModelOrchestratorState(
            fsm_state=EnumSwarmOrchestratorState.RECEIVED,
            run_id=request.run_id,
            correlation_id=request.correlation_id,
            original_task=request.task,
        )
        try:
            state, publishes = self.transition_received(state, request)
            await self._flush(publishes)
            return self._build_result(state, request)
        except Exception as exc:
            logger.error(
                "[SWARM-DISPATCH] ASYNC FAILED run_id=%s error=%s", request.run_id, exc
            )
            failed_state = state.with_error(str(exc))
            _, fail_publishes = self.transition_failed(state, str(exc), request)
            await self._flush(fail_publishes)
            return self._build_result(failed_state, request)

    # ------------------------------------------------------------------
    # FSM transition methods — synchronous, return (state, publishes)
    # ------------------------------------------------------------------

    def transition_received(
        self,
        state: ModelOrchestratorState,
        request: ModelSwarmDispatchRequest,
    ) -> tuple[ModelOrchestratorState, list[_PendingPublish]]:
        """RECEIVED: build swarm-check-endpoint-health command payload."""
        self._assert_state(state, EnumSwarmOrchestratorState.RECEIVED)
        cmd = ModelSwarmHealthCheckCommand(
            endpoint_ids=request.endpoint_ids,
            correlation_id=request.correlation_id,
            run_id=request.run_id,
        )
        publishes: list[_PendingPublish] = [(self._topic_health_cmd, cmd.model_dump())]
        logger.debug(
            "[SWARM-DISPATCH] RECEIVED → health check queued run_id=%s", request.run_id
        )
        return state, publishes

    def transition_health_checked(
        self,
        state: ModelOrchestratorState,
        health_event: dict[str, Any],
        planner_output: str = "",
        planner_model_id: str = "unknown",
    ) -> tuple[ModelOrchestratorState, list[_PendingPublish]]:
        """HEALTH_CHECKED: parse health event → build swarm-decompose command."""
        self._assert_state(state, EnumSwarmOrchestratorState.RECEIVED)
        endpoint_health = self._parse_health_event(health_event)
        state = state.with_health(endpoint_health)

        endpoint_ids = tuple(endpoint_health.keys())
        planner_hash = _hash_text(planner_output) if planner_output else ""
        cmd = ModelSwarmDecomposeCommand(
            planner_output=planner_output,
            planner_model_id=planner_model_id,
            planner_output_hash=planner_hash,
            endpoint_ids=endpoint_ids,
            original_task=state.original_task,
            correlation_id=state.correlation_id,
            run_id=state.run_id,
            decompose=True,
            max_subtasks=5,
        )
        publishes: list[_PendingPublish] = [
            (self._topic_decompose_cmd, cmd.model_dump())
        ]
        logger.debug(
            "[SWARM-DISPATCH] HEALTH_CHECKED → decompose queued run_id=%s",
            state.run_id,
        )
        return state, publishes

    def transition_decomposed(
        self,
        state: ModelOrchestratorState,
        decompose_event: dict[str, Any],
    ) -> tuple[ModelOrchestratorState, list[_PendingPublish]]:
        """DECOMPOSED: parse decomposition event → build swarm-select-endpoints command."""
        self._assert_state(state, EnumSwarmOrchestratorState.HEALTH_CHECKED)
        subtasks = self._parse_decompose_event(decompose_event)
        state = state.with_subtasks(subtasks)

        cmd = ModelSwarmSelectEndpointsCommand(
            subtasks=subtasks,
            endpoint_health=state.endpoint_health,
            correlation_id=state.correlation_id,
            run_id=state.run_id,
        )
        publishes: list[_PendingPublish] = [(self._topic_select_cmd, cmd.model_dump())]
        logger.debug(
            "[SWARM-DISPATCH] DECOMPOSED → select-endpoints queued run_id=%s subtasks=%d",
            state.run_id,
            len(subtasks),
        )
        return state, publishes

    def transition_endpoints_selected(
        self,
        state: ModelOrchestratorState,
        select_event: dict[str, Any],
    ) -> tuple[ModelOrchestratorState, list[_PendingPublish]]:
        """ENDPOINTS_SELECTED: parse selection event → build swarm-fanout command."""
        self._assert_state(state, EnumSwarmOrchestratorState.DECOMPOSED)
        assignments = self._parse_select_event(select_event)
        state = state.with_assignments(assignments)

        config = ModelSwarmConfig()
        cmd = ModelSwarmFanoutCommand(
            subtasks=state.subtasks,
            assignments=assignments,
            endpoint_health=state.endpoint_health,
            config=config,
            correlation_id=state.correlation_id,
            run_id=state.run_id,
        )
        publishes: list[_PendingPublish] = [(self._topic_fanout_cmd, cmd.model_dump())]
        logger.debug(
            "[SWARM-DISPATCH] ENDPOINTS_SELECTED → fanout queued run_id=%s assignments=%d",
            state.run_id,
            len(assignments),
        )
        return state, publishes

    def transition_dispatching(
        self,
        state: ModelOrchestratorState,
        fanout_event: dict[str, Any],
        synthesis_output: str | None = None,
    ) -> tuple[ModelOrchestratorState, list[_PendingPublish]]:
        """DISPATCHING: parse fanout event → build swarm-aggregate command."""
        self._assert_state(state, EnumSwarmOrchestratorState.ENDPOINTS_SELECTED)
        dispatches = self._parse_fanout_event(fanout_event)
        state = state.with_dispatches(dispatches)

        mode = (
            EnumAggregationMode.SYNTHESIS
            if synthesis_output
            else EnumAggregationMode.CONCATENATION
        )
        cmd = ModelSwarmAggregateCommand(
            subtasks=state.subtasks,
            dispatches_json=str([d.model_dump() for d in dispatches]),
            mode=mode,
            synthesis_output=synthesis_output,
            correlation_id=state.correlation_id,
            run_id=state.run_id,
        )
        publishes: list[_PendingPublish] = [
            (self._topic_aggregate_cmd, cmd.model_dump())
        ]
        logger.debug(
            "[SWARM-DISPATCH] DISPATCHING → aggregate queued run_id=%s dispatches=%d",
            state.run_id,
            len(dispatches),
        )
        return state, publishes

    def transition_aggregating(
        self,
        state: ModelOrchestratorState,
        aggregate_event: dict[str, Any],
        total_latency_ms: int = 0,
    ) -> tuple[ModelOrchestratorState, list[_PendingPublish]]:
        """AGGREGATING: receive aggregation-completed event — no downstream command."""
        self._assert_state(state, EnumSwarmOrchestratorState.DISPATCHING)
        aggregated_output = aggregate_event.get("aggregated_output", "")
        state = state.with_aggregated(aggregated_output, total_latency_ms)
        logger.debug(
            "[SWARM-DISPATCH] AGGREGATING run_id=%s output_len=%d",
            state.run_id,
            len(aggregated_output),
        )
        return state, []

    def transition_completed(
        self,
        state: ModelOrchestratorState,
    ) -> tuple[ModelOrchestratorState, list[_PendingPublish]]:
        """AGGREGATING → COMPLETED: build terminal swarm-dispatch-completed event."""
        self._assert_state(state, EnumSwarmOrchestratorState.AGGREGATING)
        state = state.with_state(EnumSwarmOrchestratorState.COMPLETED)
        payload = self._build_completed_payload(state)
        publishes: list[_PendingPublish] = [(self._topic_completed, payload)]
        logger.info("[SWARM-DISPATCH] COMPLETED run_id=%s", state.run_id)
        return state, publishes

    def transition_failed(
        self,
        state: ModelOrchestratorState,
        error: str,
        request: ModelSwarmDispatchRequest | None = None,
    ) -> tuple[ModelOrchestratorState, list[_PendingPublish]]:
        """Any state → FAILED: build terminal swarm-dispatch-failed event."""
        state = state.with_error(error)
        payload: dict[str, Any] = {
            "run_id": state.run_id,
            "correlation_id": state.correlation_id,
            "fsm_state": state.fsm_state.value,
            "error": state.error,
            "task": request.task if request else state.original_task,
        }
        publishes: list[_PendingPublish] = [(self._topic_failed, payload)]
        logger.error("[SWARM-DISPATCH] FAILED run_id=%s error=%s", state.run_id, error)
        return state, publishes

    # ------------------------------------------------------------------
    # Event parsing helpers
    # ------------------------------------------------------------------

    def _parse_health_event(
        self, event: dict[str, Any]
    ) -> dict[str, ModelEndpointHealth]:
        result: dict[str, ModelEndpointHealth] = {}
        raw: dict[str, Any] = event.get("endpoint_health", {})
        for eid, data in raw.items():
            result[eid] = ModelEndpointHealth(
                endpoint_id=eid,
                status=data.get("endpoint_status", "unknown"),
                latency_ms=data.get("latency_ms"),
                error=data.get("error"),
            )
        return result

    def _parse_decompose_event(self, event: dict[str, Any]) -> tuple[ModelSubtask, ...]:
        raw: list[dict[str, Any]] = event.get("subtasks", [])
        return tuple(
            ModelSubtask(
                subtask_id=s["subtask_id"],
                description=s.get("description", ""),
                model_affinity=s.get("model_affinity", ""),
                depends_on=tuple(s.get("depends_on", [])),
                estimated_tokens=s.get("estimated_tokens", 0),
                category=s.get("category", "general"),
            )
            for s in raw
        )

    def _parse_select_event(self, event: dict[str, Any]) -> dict[str, str]:
        assignments: dict[str, Any] = event.get("assignments", {})
        return {k: str(v) for k, v in assignments.items()}

    def _parse_fanout_event(
        self, event: dict[str, Any]
    ) -> tuple[ModelSubtaskDispatch, ...]:
        raw: list[dict[str, Any]] = event.get("dispatches", [])
        return tuple(
            ModelSubtaskDispatch(
                subtask_id=d["subtask_id"],
                endpoint_id=d.get("endpoint_id", ""),
                status=d.get("status", "failed"),
                latency_ms=d.get("latency_ms", 0),
                result_text=d.get("result_text", ""),
                failure_reason=d.get("failure_reason", ""),
                wave=d.get("wave", 0),
                model_id=d.get("model_id", ""),
                base_url=d.get("base_url", ""),
            )
            for d in raw
        )

    # ------------------------------------------------------------------
    # Terminal payload builder
    # ------------------------------------------------------------------

    def _build_completed_payload(self, state: ModelOrchestratorState) -> dict[str, Any]:
        succeeded = sum(
            1 for d in state.dispatches if d.status == EnumSubtaskStatus.SUCCEEDED
        )
        failed = sum(1 for d in state.dispatches if d.status in _FAILED_STATUSES)
        skipped = sum(
            1
            for d in state.dispatches
            if d.status == EnumSubtaskStatus.SKIPPED_DEPENDENCY_FAILED
        )
        run_status = self._determine_run_status(state.dispatches)
        models_used = sorted({d.model_id for d in state.dispatches if d.model_id})
        return {
            "run_id": state.run_id,
            "correlation_id": state.correlation_id,
            "status": run_status.value,
            "aggregated_output": state.aggregated_output,
            "subtask_count": len(state.dispatches),
            "succeeded_count": succeeded,
            "failed_count": failed,
            "skipped_count": skipped,
            "total_latency_ms": state.total_latency_ms,
            "models_used": list(models_used),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _assert_state(
        self,
        state: ModelOrchestratorState,
        expected: EnumSwarmOrchestratorState,
    ) -> None:
        if state.fsm_state != expected:
            raise InvalidFSMTransitionError(
                f"Expected FSM state {expected!r}, got {state.fsm_state!r}"
            )

    async def _flush(self, publishes: list[_PendingPublish]) -> None:
        if self._event_bus is None:
            return
        for topic, payload in publishes:
            if not topic:
                continue
            value = json.dumps(payload).encode()
            await self._event_bus.publish(topic=topic, key=None, value=value)

    def _determine_run_status(
        self, dispatches: tuple[ModelSubtaskDispatch, ...]
    ) -> EnumSwarmRunStatus:
        failed = [d for d in dispatches if d.status in _FAILED_STATUSES]
        succeeded = [d for d in dispatches if d.status == EnumSubtaskStatus.SUCCEEDED]
        if not failed:
            return EnumSwarmRunStatus.SUCCEEDED
        if succeeded:
            return EnumSwarmRunStatus.DEGRADED
        return EnumSwarmRunStatus.FAILED

    def _build_result(
        self,
        state: ModelOrchestratorState,
        request: ModelSwarmDispatchRequest,
    ) -> ModelSwarmDispatchResult:
        if state.fsm_state == EnumSwarmOrchestratorState.FAILED:
            return ModelSwarmDispatchResult(
                run_id=state.run_id,
                correlation_id=state.correlation_id,
                status=EnumSwarmRunStatus.FAILED,
                aggregated_output="",
                subtask_count=0,
                succeeded_count=0,
                failed_count=0,
                skipped_count=0,
                total_latency_ms=0,
                error=state.error,
            )
        succeeded = sum(
            1 for d in state.dispatches if d.status == EnumSubtaskStatus.SUCCEEDED
        )
        failed = sum(1 for d in state.dispatches if d.status in _FAILED_STATUSES)
        skipped = sum(
            1
            for d in state.dispatches
            if d.status == EnumSubtaskStatus.SKIPPED_DEPENDENCY_FAILED
        )
        models_used = tuple(
            sorted({d.model_id for d in state.dispatches if d.model_id})
        )
        run_status = self._determine_run_status(state.dispatches)
        return ModelSwarmDispatchResult(
            run_id=state.run_id,
            correlation_id=state.correlation_id,
            status=run_status,
            aggregated_output=state.aggregated_output,
            subtask_count=len(state.dispatches),
            succeeded_count=succeeded,
            failed_count=failed,
            skipped_count=skipped,
            total_latency_ms=state.total_latency_ms,
            models_used=models_used,
        )
