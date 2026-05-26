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

Multi-topic routing (OMN-12003):
  The orchestrator subscribes to 6 topics (one command + 5 response events).
  Each topic maps to a distinct FSM transition via `_TOPIC_TRANSITION_MAP`.
  FSM state is persisted per run_id via `ProtocolStateStore` (injected) so
  that each incoming event can resume the correct in-flight run.

  Entry points:
    - ``handle_async(request)``  — called for the initial swarm-dispatch command
    - ``route_event(topic, payload)`` — called for subsequent response events;
      loads FSM state from store, advances it, persists new state, flushes publishes
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
    from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )
    from omnibase_core.protocols.storage.protocol_state_store import ProtocolStateStore

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

# Node-id used as the namespace key in ProtocolStateStore
_STATE_NODE_ID = "node_swarm_dispatch_orchestrator"


def _load_contract(contract_path: Path | None = None) -> dict[str, Any]:
    path = contract_path or Path(__file__).parent.parent / "contract.yaml"
    with open(path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class InvalidFSMTransitionError(Exception):
    pass


class MissingRunStateError(Exception):
    """Raised when a response event arrives for an unknown run_id."""


class HandlerSwarmDispatchOrchestrator:
    """FSM orchestrator for swarm dispatch.

    All transition_* methods are synchronous and return (new_state, publishes).
    The `publishes` list contains (topic, payload) pairs to emit after the
    transition. This keeps the FSM logic unit-testable via a MagicMock bus.

    ``handle`` runs the FSM and discards all pending publishes (test/standalone mode).
    ``handle_async`` handles the initial swarm-dispatch command: creates run state,
    calls transition_received, persists state, flushes publishes.
    ``route_event`` handles subsequent response events: loads run state from store,
    routes to the matching transition, persists updated state, flushes publishes.

    Topic → transition routing (loaded from contract.yaml subscribe_topics):
      - swarm-dispatch              → handle_async (initial command; creates new run)
      - swarm-endpoint-health-completed → transition_health_checked
      - swarm-decomposition-completed   → transition_decomposed
      - swarm-endpoints-selected        → transition_endpoints_selected
      - swarm-fanout-completed          → transition_dispatching → persist
      - swarm-aggregation-completed     → transition_aggregating → transition_completed

    State is persisted in a class-level in-process dict (_in_process_state) keyed
    by run_id. This allows FSM continuations across separate dispatcher invocations
    within the same process when ProtocolStateStore is not available.
    """

    # In-process FSM state store — shared across all instances in this process.
    # Keyed by run_id. Used as fallback when _state_store is None.
    _in_process_state: dict[str, ModelOrchestratorState] = {}

    def __init__(
        self,
        *,
        event_bus: ProtocolEventBusPublisher | None = None,
        state_store: ProtocolStateStore | None = None,
        contract_path: Path | None = None,
    ) -> None:
        contract = _load_contract(contract_path)
        pub: list[str] = contract.get("event_bus", {}).get("publish_topics", [])
        sub: list[str] = contract.get("event_bus", {}).get("subscribe_topics", [])
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
        self._state_store = state_store

        # Build topic → transition-key map from contract subscribe_topics.
        # Each entry maps a full topic string to a short routing key.
        self._topic_routing: dict[str, str] = {}
        for topic in sub:
            if "swarm-endpoint-health-completed" in topic:
                self._topic_routing[topic] = "health_checked"
            elif "swarm-decomposition-completed" in topic:
                self._topic_routing[topic] = "decomposed"
            elif "swarm-endpoints-selected" in topic:
                self._topic_routing[topic] = "endpoints_selected"
            elif "swarm-fanout-completed" in topic:
                self._topic_routing[topic] = "dispatching"
            elif "swarm-aggregation-completed" in topic:
                self._topic_routing[topic] = "aggregating"

        # Build event_type alias → routing key for envelope-based dispatch.
        # Alias format: "{parts[2]}.{parts[3]}" from "onex.evt.{ns}.{name}.v1"
        self._event_type_routing: dict[str, str] = {}
        for full_topic, routing_key in self._topic_routing.items():
            parts = full_topic.split(".")
            if len(parts) >= 5 and parts[0] == "onex":
                alias = f"{parts[2]}.{parts[3]}"
                self._event_type_routing[alias] = routing_key

    @property
    def subscribed_event_topics(self) -> tuple[str, ...]:
        """Return the response event topics that this handler routes via route_event."""
        return tuple(self._topic_routing.keys())

    # ------------------------------------------------------------------
    # Sync entry point (no bus; used in tests and standalone mode)
    # ------------------------------------------------------------------

    def handle(
        self, request: ModelSwarmDispatchRequest
    ) -> ModelSwarmDispatchResult | None:
        """Drive FSM; delegates to handle_async when event_bus is available (OMN-12151)."""
        if self._event_bus is not None:
            import asyncio

            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already inside an async context — schedule and return None to
                # suppress the terminal event publish (result_applier skips on None).
                _task = loop.create_task(self.handle_async(request))
                _ = _task  # retain reference per RUF006
                return None
            return loop.run_until_complete(self.handle_async(request))
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
            max_subtasks=request.max_subtasks,
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

    async def handle_async(self, request: object) -> None:
        """Unified async entry point: handles initial command or FSM response events.

        When called with a ModelSwarmDispatchRequest (initial swarm-dispatch cmd),
        creates FSM state and emits the health-check command.

        When called with a ModelEventEnvelope (FSM response event — auto-wiring
        dispatches envelopes when no event_model is declared in handler_routing),
        routes to route_event_by_type using envelope.event_type.

        Returns None in all cases (OMN-12151): the FSM terminal event is emitted
        by route_event when aggregating → COMPLETED fires.
        """
        from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

        if isinstance(request, ModelSwarmDispatchRequest):
            await self._handle_async_initial(request)
            return

        if isinstance(request, ModelEventEnvelope):
            event_type = str(getattr(request, "event_type", "") or "")
            payload = request.payload
            payload_dict: dict[str, Any] = (
                payload
                if isinstance(payload, dict)
                else (payload.model_dump() if hasattr(payload, "model_dump") else {})
            )
            await self.route_event_by_type(event_type, payload_dict)
            return

        # MDE materializes envelopes as dicts before dispatch (ModelMaterializedDispatch).
        # When event_model is None in handler_routing, the callback receives this dict.
        # Schema: {payload: dict, __bindings: dict, __debug_trace: {event_type, ...}}
        if isinstance(request, dict):
            debug_trace = request.get("__debug_trace") or {}
            event_type_raw = (
                debug_trace.get("event_type") if isinstance(debug_trace, dict) else None
            )
            event_type = str(event_type_raw or "")
            payload_dict = request.get("payload") or {}
            if not isinstance(payload_dict, dict):
                payload_dict = {}
            if event_type:
                logger.debug(
                    "[SWARM-DISPATCH] handle_async: materialized dict event_type=%s",
                    event_type,
                )
                await self.route_event_by_type(event_type, payload_dict)
                return
            logger.warning(
                "[SWARM-DISPATCH] handle_async: materialized dict missing event_type — ignoring payload keys=%s",
                list(request.keys()),
            )
            return

        logger.warning(
            "[SWARM-DISPATCH] handle_async: unexpected request type=%s — ignoring",
            type(request).__name__,
        )

    async def _handle_async_initial(self, request: ModelSwarmDispatchRequest) -> None:
        """Handle the initial swarm-dispatch command.

        Creates fresh FSM state for the run_id, emits the health-check command,
        and persists state so subsequent response events can resume this run.

        Returns None (OMN-12151): the FSM is not complete after RECEIVED — the
        orchestrator has only enqueued the swarm-check-endpoint-health sub-command.
        Returning None suppresses DispatchResultApplier from publishing a terminal
        event prematurely, before the full FSM traversal (RECEIVED → HEALTH_CHECKED
        → DECOMPOSED → ENDPOINTS_SELECTED → DISPATCHING → AGGREGATING → COMPLETED)
        is finished via subsequent ``route_event`` calls on response topics.
        """
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
            max_subtasks=request.max_subtasks,
        )
        try:
            state, publishes = self.transition_received(state, request)
            await self._persist_state(state)
            await self._flush(publishes)
            # Return None: RECEIVED is non-terminal. The terminal event is emitted
            # by route_event when aggregating → COMPLETED fires.
            return
        except Exception as exc:
            logger.error(
                "[SWARM-DISPATCH] ASYNC FAILED run_id=%s error=%s", request.run_id, exc
            )
            _, fail_publishes = self.transition_failed(state, str(exc), request)
            await self._flush(fail_publishes)
            # Return None even on failure: transition_failed already published
            # the swarm-dispatch-failed terminal event via _flush above.
            return

    async def route_event_by_type(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> ModelOrchestratorState | None:
        """Route an FSM response event using event_type alias instead of full topic.

        Called by handle_async when auto-wiring dispatches a ModelEventEnvelope
        for FSM continuation events (no event_model in handler_routing entry).

        Args:
            event_type: envelope.event_type, e.g. "omnimarket.swarm-endpoint-health-completed"
            payload:    decoded event payload dict (must contain run_id)
        """
        routing_key = self._event_type_routing.get(event_type, "")
        if not routing_key:
            logger.warning(
                "[SWARM-DISPATCH] route_event_by_type: unmapped event_type=%s",
                event_type,
            )
            return None

        # Find the matching full topic from _topic_routing (reverse lookup for logging)
        topic = next(
            (t for t, k in self._topic_routing.items() if k == routing_key), event_type
        )
        return await self.route_event(topic, payload)

    # ------------------------------------------------------------------
    # Multi-topic event router (OMN-12003)
    # ------------------------------------------------------------------

    async def route_event(
        self,
        topic: str,
        payload: dict[str, Any],
    ) -> ModelOrchestratorState:
        """Route a response event to the correct FSM transition.

        Looks up the run_id from payload, loads persisted FSM state, dispatches
        to the appropriate transition_* method, persists the new state, and
        flushes pending publishes.

        Args:
            topic:   The Kafka topic the event arrived on.
            payload: Decoded event payload (must contain ``run_id``).

        Returns:
            The updated FSM state after the transition.

        Raises:
            MissingRunStateError: No persisted state found for the run_id.
            InvalidFSMTransitionError: Transition guard rejected current state.
        """
        run_id: str = payload.get("run_id", "")
        if not run_id:
            logger.warning("[SWARM-DISPATCH] route_event: missing run_id in payload")
            raise ValueError("route_event payload missing run_id")

        routing_key = self._topic_routing.get(topic, "")
        if not routing_key:
            logger.warning(
                "[SWARM-DISPATCH] route_event: unmapped topic=%s run_id=%s",
                topic,
                run_id,
            )
            raise ValueError(f"No FSM routing for topic: {topic!r}")

        state = await self._load_state(run_id)
        if state is None:
            raise MissingRunStateError(
                f"No FSM state for run_id={run_id!r} on topic={topic!r}"
            )

        logger.info(
            "[SWARM-DISPATCH] route_event run_id=%s topic=%s routing_key=%s "
            "current_state=%s",
            run_id,
            topic,
            routing_key,
            state.fsm_state.value,
        )

        publishes: list[_PendingPublish]

        if routing_key == "health_checked":
            state, publishes = self.transition_health_checked(state, payload)
        elif routing_key == "decomposed":
            state, publishes = self.transition_decomposed(state, payload)
        elif routing_key == "endpoints_selected":
            state, publishes = self.transition_endpoints_selected(state, payload)
        elif routing_key == "dispatching":
            state, publishes = self.transition_dispatching(state, payload)
        elif routing_key == "aggregating":
            # aggregation-completed advances through AGGREGATING → COMPLETED in one shot
            state, _ = self.transition_aggregating(state, payload)
            state, publishes = self.transition_completed(state)
        else:
            raise ValueError(f"Unhandled routing_key: {routing_key!r}")

        await self._persist_state(state)
        await self._flush(publishes)
        return state

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
        # ModelSwarmDecomposeRequest requires min_length=1 on planner_output/hash.
        # When no LLM planner ran (standard case), use the original task as the
        # planner output — the decomposer handler falls through to passthrough mode
        # when original_task is shorter than token_threshold (default 2000 chars).
        effective_planner_output = (
            planner_output or state.original_task or "passthrough"
        )
        effective_planner_model_id = (
            planner_model_id if planner_model_id != "unknown" else "passthrough"
        )
        planner_hash = _hash_text(effective_planner_output)
        cmd = ModelSwarmDecomposeCommand(
            planner_output=effective_planner_output,
            planner_model_id=effective_planner_model_id,
            planner_output_hash=planner_hash,
            endpoint_ids=endpoint_ids,
            original_task=state.original_task,
            correlation_id=state.correlation_id,
            run_id=state.run_id,
            decompose=True,
            max_subtasks=state.max_subtasks,
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
        wall_latency_ms = int(fanout_event.get("wall_latency_ms", 0))
        state = state.with_dispatches(
            dispatches, dispatch_wall_latency_ms=wall_latency_ms
        )

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
    # State persistence helpers (OMN-12003)
    # ------------------------------------------------------------------

    async def _persist_state(self, state: ModelOrchestratorState) -> None:
        """Persist FSM state — ProtocolStateStore if available, else in-process dict."""
        HandlerSwarmDispatchOrchestrator._in_process_state[state.run_id] = state
        if self._state_store is not None:
            from datetime import UTC, datetime

            from omnibase_core.models.state.model_state_envelope import (
                ModelStateEnvelope,
            )

            envelope = ModelStateEnvelope(
                node_id=_STATE_NODE_ID,
                scope_id=state.run_id,
                data=state.model_dump(mode="json"),
                written_at=datetime.now(UTC),
            )
            await self._state_store.put(envelope)
        logger.debug(
            "[SWARM-DISPATCH] state persisted run_id=%s fsm_state=%s",
            state.run_id,
            state.fsm_state.value,
        )

    async def _load_state(self, run_id: str) -> ModelOrchestratorState | None:
        """Load FSM state — in-process dict first (fastest), then ProtocolStateStore."""
        in_proc = HandlerSwarmDispatchOrchestrator._in_process_state.get(run_id)
        if in_proc is not None:
            return in_proc
        if self._state_store is None:
            return None

        raw_envelope: ModelStateEnvelope | None = await self._state_store.get(
            _STATE_NODE_ID, scope_id=run_id
        )
        if raw_envelope is None:
            return None
        return ModelOrchestratorState.model_validate(raw_envelope.data)

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
        wall_ms = state.dispatch_wall_latency_ms
        sum_subtask_ms = sum(d.latency_ms for d in state.dispatches)
        # Cloud T4 cost proxy: $0.376/hr → ~$1.044e-7/ms per subtask
        _t4_cost_per_ms = 1.044e-7
        cloud_equivalent_cost_usd = round(sum_subtask_ms * _t4_cost_per_ms, 6)
        total_cost_usd = 0.0
        savings_usd = round(cloud_equivalent_cost_usd - total_cost_usd, 6)
        speedup = (
            round(sum_subtask_ms / wall_ms, 4)
            if wall_ms > 0 and sum_subtask_ms > wall_ms
            else 1.0
        )
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
            "dispatch_wall_latency_ms": wall_ms,
            "total_cost_usd": total_cost_usd,
            "cloud_equivalent_cost_usd": cloud_equivalent_cost_usd,
            "savings_usd": savings_usd,
            "parallelism_speedup_ratio": speedup,
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
        """Publish pending commands as ModelEventEnvelope-wrapped messages.

        Wraps each payload in an envelope so consumers using _make_event_bus_callback
        can deserialize via ModelEventEnvelope.model_validate without a fallback.
        """
        import uuid as _uuid
        from datetime import UTC, datetime

        if self._event_bus is None:
            return
        for topic, payload in publishes:
            if not topic:
                continue
            # Derive event_type alias from topic (e.g. "omnimarket.swarm-decompose")
            parts = topic.split(".")
            event_type = f"{parts[2]}.{parts[3]}" if len(parts) >= 5 else topic
            # Correlation/run id from payload for tracing
            corr_id = payload.get("correlation_id") or str(_uuid.uuid4())
            envelope_dict = {
                "payload": payload,
                "correlation_id": corr_id,
                "event_type": event_type,
                "source_node": "node_swarm_dispatch_orchestrator",
                "envelope_timestamp": datetime.now(UTC).isoformat(),
                "schema_version": "1.0.0",
            }
            value = json.dumps(envelope_dict).encode()
            logger.info(
                "[SWARM-DISPATCH] _flush: publishing to topic=%s corr=%s",
                topic,
                corr_id,
            )
            await self._event_bus.publish(topic=topic, key=None, value=value)
            logger.info("[SWARM-DISPATCH] _flush: published ok topic=%s", topic)

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
