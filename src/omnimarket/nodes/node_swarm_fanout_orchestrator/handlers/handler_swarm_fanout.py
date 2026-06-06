# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wave-scheduled swarm fan-out orchestrator.

Publishes delegation-execute commands per subtask and collects
delegation completion events by correlation_id. No HTTP. No threads.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Protocol

import yaml

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.enums import (
    EnumExecutionStatus,
    EnumFanoutFsmState,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_subtask import (
    ModelSubtask,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_dispatch import (
    ModelSwarmDispatch,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_endpoint import (
    ModelSwarmEndpoint,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_fanout_request import (
    ModelSwarmFanoutRequest,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_fanout_result import (
    ModelSwarmFanoutResult,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

_DEFAULT_REGISTRY_PATH = (
    Path(__file__).parent.parent.parent
    / "node_swarm_registry_compute"
    / "contracts"
    / "endpoint_registry.yaml"
)


class ProtocolQueuePublisher(Protocol):
    def publish(self, topic: str, payload: dict[str, object]) -> None: ...


class ProtocolQueueSubscriber(Protocol):
    def poll(
        self, topics: list[str], run_id: str, timeout_seconds: float
    ) -> list[dict[str, object]]: ...


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _compute_waves(subtasks: tuple[ModelSubtask, ...]) -> list[list[ModelSubtask]]:
    id_to_subtask = {s.subtask_id: s for s in subtasks}
    assigned: dict[str, int] = {}

    def _wave_of(subtask_id: str) -> int:
        if subtask_id in assigned:
            return assigned[subtask_id]
        subtask = id_to_subtask[subtask_id]
        if not subtask.depends_on:
            assigned[subtask_id] = 0
            return 0
        wave = max(_wave_of(dep) for dep in subtask.depends_on) + 1
        assigned[subtask_id] = wave
        return wave

    for s in subtasks:
        _wave_of(s.subtask_id)

    if not assigned:
        return []
    max_wave = max(assigned.values())
    waves: list[list[ModelSubtask]] = [[] for _ in range(max_wave + 1)]
    for s in subtasks:
        waves[assigned[s.subtask_id]].append(s)
    for wave in waves:
        wave.sort(key=lambda s: s.subtask_id)
    return waves


def _load_endpoint_registry(
    registry_path: Path | None = None,
) -> dict[str, ModelSwarmEndpoint]:
    path = registry_path or _DEFAULT_REGISTRY_PATH
    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text())
        endpoints: dict[str, ModelSwarmEndpoint] = {}
        for ep_data in raw.get("endpoints", []):
            ep_id = str(ep_data.get("id", ""))
            if not ep_id:
                continue
            endpoints[ep_id] = ModelSwarmEndpoint(
                endpoint_id=ep_id,
                base_url=str(ep_data.get("base_url", "")),
                model_id=str(ep_data.get("model_id", "")),
                endpoint_ref=str(ep_data.get("endpoint_ref", "")),
                capabilities=tuple(str(c) for c in ep_data.get("capabilities", [])),
                status="reachable",
            )
        return endpoints
    except Exception as exc:
        logger.warning("Failed to load endpoint registry from %s: %s", path, exc)
        return {}


def _correlation_id(run_id: str, subtask_id: str) -> str:
    return f"{run_id}-{subtask_id}"


def _owns_correlation_id(run_id: str, correlation_id: str) -> bool:
    """True if this correlation_id was issued by this fanout run."""
    return correlation_id.startswith(f"{run_id}-")


class HandlerSwarmFanout:
    """FSM orchestrator: PLANNING → DISPATCHING → COLLECTING → COMPLETED/FAILED.

    No HTTP. No threads. Publishes delegation-execute commands and collects
    delegation completion events. The delegation call effect handles all I/O.
    """

    def __init__(
        self,
        queue_publisher: ProtocolQueuePublisher | None = None,
        queue_subscriber: ProtocolQueueSubscriber | None = None,
        registry_path: Path | None = None,
    ) -> None:
        # Provide no-op stubs when queue infra is not injected (lab/test mode).
        # Subtasks with no endpoint assignment take the passthrough path anyway.
        class _StubPublisher:
            def publish(self, topic: str, payload: dict[str, object]) -> None:
                logger.debug(
                    "No queue publisher configured; dropping fanout command",
                    extra={"topic": topic, "payload_keys": sorted(payload)},
                )

        class _StubSubscriber:
            def poll(
                self, topics: list[str], run_id: str, timeout_seconds: float
            ) -> list[dict[str, object]]:
                return []

        self._queue_publisher = queue_publisher or _StubPublisher()
        self._queue_subscriber = queue_subscriber or _StubSubscriber()
        self._registry_path = registry_path

    def _resolve_endpoint_map(
        self, request: ModelSwarmFanoutRequest
    ) -> dict[str, ModelSwarmEndpoint]:
        if request.endpoints:
            return {ep.endpoint_id: ep for ep in request.endpoints}
        if request.endpoint_health:
            registry = _load_endpoint_registry(self._registry_path)
            resolved: dict[str, ModelSwarmEndpoint] = {}
            for ep_id in request.endpoint_health:
                ep = registry.get(ep_id)
                if ep is not None:
                    resolved[ep_id] = ep
                else:
                    logger.warning("endpoint_id %r not found in registry", ep_id)
            return resolved
        return {}

    def handle(self, request: ModelSwarmFanoutRequest) -> ModelSwarmFanoutResult:
        publish_topics = contract_publish_topics(_CONTRACT_PATH)
        delegation_execute_topic = next(
            t for t in publish_topics if "delegation-execute" in t
        )
        subscribe_topics_all = contract_subscribe_topics(_CONTRACT_PATH)
        completion_topics = [
            t
            for t in subscribe_topics_all
            if any(
                k in t
                for k in [
                    "delegation-call-completed",
                    "delegation-escalation-triggered",
                    "delegation-all-tiers-failed",
                ]
            )
        ]

        # PLANNING: validate inputs, build endpoint map and wave schedule
        fsm_state = EnumFanoutFsmState.PLANNING
        endpoint_map = self._resolve_endpoint_map(request)
        waves = _compute_waves(request.subtasks)
        config = request.config
        total_deadline = time.monotonic() + config.total_run_timeout_seconds
        all_dispatches: list[ModelSwarmDispatch] = []
        failed_ids: set[str] = set()
        wall_start = time.monotonic()

        # DISPATCHING: publish commands wave-by-wave, collect after each wave
        fsm_state = EnumFanoutFsmState.DISPATCHING

        for wave_num, wave_subtasks in enumerate(waves):
            runnable: list[ModelSubtask] = []
            for subtask in wave_subtasks:
                if any(dep in failed_ids for dep in subtask.depends_on):
                    all_dispatches.append(
                        ModelSwarmDispatch(
                            subtask_id=subtask.subtask_id,
                            endpoint_id="",
                            model_id="",
                            base_url="",
                            execution_status=EnumExecutionStatus.SKIPPED_DEPENDENCY_FAILED,
                            failure_reason="dependency_failed",
                            wave=wave_num,
                        )
                    )
                    failed_ids.add(subtask.subtask_id)
                else:
                    runnable.append(subtask)

            if not runnable:
                continue

            wave_dispatches = self._dispatch_wave(
                wave_num=wave_num,
                subtasks=runnable,
                request=request,
                endpoint_map=endpoint_map,
                delegation_execute_topic=delegation_execute_topic,
                completion_topics=completion_topics,
                total_deadline=total_deadline,
            )
            for d in wave_dispatches:
                all_dispatches.append(d)
                if d.execution_status != EnumExecutionStatus.SUCCEEDED:
                    failed_ids.add(d.subtask_id)

        fsm_state = EnumFanoutFsmState.COMPLETED
        wall_latency_ms = int((time.monotonic() - wall_start) * 1000)
        sum_latency = sum(d.latency_ms for d in all_dispatches)
        completed_count = sum(
            1
            for d in all_dispatches
            if d.execution_status == EnumExecutionStatus.SUCCEEDED
        )
        failed_count = len(all_dispatches) - completed_count
        _ = fsm_state

        return ModelSwarmFanoutResult(
            dispatches=tuple(all_dispatches),
            wall_latency_ms=wall_latency_ms,
            sum_subtask_latency_ms=sum_latency,
            run_id=request.run_id,
            completed_count=completed_count,
            failed_count=failed_count,
            degraded=failed_count > 0,
            aggregation_mode="collect_all",
            endpoint_registry_hash=request.endpoint_registry_hash,
            routing_policy_hash=request.routing_policy_hash,
        )

    def _dispatch_wave(
        self,
        wave_num: int,
        subtasks: list[ModelSubtask],
        request: ModelSwarmFanoutRequest,
        endpoint_map: dict[str, ModelSwarmEndpoint],
        delegation_execute_topic: str,
        completion_topics: list[str],
        total_deadline: float,
    ) -> list[ModelSwarmDispatch]:
        assert self._queue_publisher is not None
        assert self._queue_subscriber is not None

        config = request.config
        pending: dict[str, tuple[ModelSubtask, ModelSwarmEndpoint | None]] = {}

        for subtask in subtasks:
            time_left = total_deadline - time.monotonic()
            if time_left <= 0:
                continue

            endpoint_id = request.assignments.get(subtask.subtask_id, "")
            endpoint = endpoint_map.get(endpoint_id)

            corr_id = _correlation_id(request.run_id, subtask.subtask_id)

            if endpoint is None or not endpoint.endpoint_ref:
                pending[corr_id] = (subtask, None)
                continue

            command: dict[str, object] = {
                "request_id": f"{request.run_id}-{subtask.subtask_id}",
                "correlation_id": corr_id,
                "causation_id": request.run_id,
                "model_id": endpoint.model_id,
                "endpoint_ref": endpoint.endpoint_ref,
                "prompt": subtask.description,
                "prompt_hash": _sha256(subtask.description),
                "max_tokens": subtask.estimated_tokens or 2048,
                "task_type": "swarm_subtask",
                "task_id": subtask.subtask_id,
                "routing_policy_hash": request.routing_policy_hash,
                "registry_hash": request.endpoint_registry_hash,
            }
            self._queue_publisher.publish(delegation_execute_topic, command)
            pending[corr_id] = (subtask, endpoint)

        if not pending:
            return []

        # COLLECTING: wait for terminal events for this wave
        time_left = max(total_deadline - time.monotonic(), 1.0)
        raw_events = self._queue_subscriber.poll(
            completion_topics,
            request.run_id,
            timeout_seconds=min(float(config.per_endpoint_timeout_seconds), time_left),
        )

        results: dict[str, dict[str, object]] = {}
        for raw_ev in raw_events:
            corr_id_val = str(raw_ev.get("correlation_id", ""))
            if not _owns_correlation_id(request.run_id, corr_id_val):
                continue
            results[corr_id_val] = raw_ev

        dispatches: list[ModelSwarmDispatch] = []
        for corr_id, (subtask, endpoint) in pending.items():
            endpoint_id = endpoint.endpoint_id if endpoint else ""
            model_id = endpoint.model_id if endpoint else ""
            base_url = endpoint.base_url if endpoint else ""

            if endpoint is None or not endpoint.endpoint_ref:
                dispatches.append(
                    ModelSwarmDispatch(
                        subtask_id=subtask.subtask_id,
                        endpoint_id=endpoint_id,
                        model_id=model_id,
                        base_url=base_url,
                        execution_status=EnumExecutionStatus.FAILED,
                        failure_reason="no_endpoint_assigned",
                        wave=wave_num,
                    )
                )
                continue

            maybe_event = results.get(corr_id)
            if maybe_event is None:
                dispatches.append(
                    ModelSwarmDispatch(
                        subtask_id=subtask.subtask_id,
                        endpoint_id=endpoint_id,
                        model_id=model_id,
                        base_url=base_url,
                        execution_status=EnumExecutionStatus.TIMEOUT,
                        failure_reason="no_terminal_event_received",
                        wave=wave_num,
                    )
                )
                continue

            event: dict[str, object] = maybe_event
            is_failure = "all-tiers-failed" in str(event.get("_topic", ""))
            success = bool(event.get("success", True)) and not is_failure
            latency_ms = int(str(event.get("latency_ms", 0)))

            dispatches.append(
                ModelSwarmDispatch(
                    subtask_id=subtask.subtask_id,
                    endpoint_id=endpoint_id,
                    model_id=str(event.get("model_id", model_id)),
                    base_url=base_url,
                    execution_status=(
                        EnumExecutionStatus.SUCCEEDED
                        if success
                        else EnumExecutionStatus.FAILED
                    ),
                    failure_reason=""
                    if success
                    else str(event.get("failure_class", "")),
                    latency_ms=latency_ms,
                    wave=wave_num,
                )
            )

        return dispatches
