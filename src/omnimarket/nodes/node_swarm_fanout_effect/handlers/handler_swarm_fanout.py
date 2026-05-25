# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wave-scheduled parallel fan-out dispatcher for swarm execution."""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from pathlib import Path
from typing import Any, Protocol

import yaml

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.enums import (
    EnumDispatchMode,
    EnumExecutionStatus,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_subtask import ModelSubtask
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_config import (
    ModelSwarmConfig,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_dispatch import (
    ModelSwarmDispatch,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_endpoint import (
    ModelSwarmEndpoint,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_fanout_request import (
    ModelSwarmFanoutRequest,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_fanout_result import (
    ModelSwarmFanoutResult,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_subtask_assignment import (
    ModelSwarmSubtaskAssignment,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_subtask_result import (
    ModelSwarmSubtaskResult,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

# Shared endpoint registry used for ID-only requests from the orchestrator.
_DEFAULT_REGISTRY_PATH = (
    Path(__file__).parent.parent.parent
    / "node_swarm_registry_compute"
    / "contracts"
    / "endpoint_registry.yaml"
)


def _load_endpoint_registry(
    registry_path: Path | None = None,
) -> dict[str, ModelSwarmEndpoint]:
    """Load the endpoint registry and return a map of endpoint_id -> ModelSwarmEndpoint."""
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
                status="reachable",
            )
        return endpoints
    except Exception as exc:
        logger.warning("Failed to load endpoint registry from %s: %s", path, exc)
        return {}


class _HttpResponse(Protocol):
    def json(self) -> dict[str, object]: ...

    @property
    def status_code(self) -> int: ...


class ProtocolHttpClient(Protocol):
    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
    ) -> _HttpResponse: ...


class ProtocolQueuePublisher(Protocol):
    def publish(self, topic: str, payload: dict[str, object]) -> None: ...


class ProtocolQueueSubscriber(Protocol):
    def poll(
        self, topic: str, run_id: str, timeout_seconds: float
    ) -> list[dict[str, object]]: ...


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


class HandlerSwarmFanout:
    def __init__(
        self,
        http_client: ProtocolHttpClient | None = None,
        queue_publisher: ProtocolQueuePublisher | None = None,
        queue_subscriber: ProtocolQueueSubscriber | None = None,
        registry_path: Path | None = None,
    ) -> None:
        self._http_client = http_client
        self._queue_publisher = queue_publisher
        self._queue_subscriber = queue_subscriber
        self._registry_path = registry_path

    def _resolve_endpoint_map(
        self, request: ModelSwarmFanoutRequest
    ) -> dict[str, ModelSwarmEndpoint]:
        """Return endpoint_id -> ModelSwarmEndpoint from request or registry."""
        if request.endpoints:
            return {ep.endpoint_id: ep for ep in request.endpoints}
        # Orchestrator sent endpoint_health dict (keyed by ID) — resolve from registry.
        if request.endpoint_health:
            registry = _load_endpoint_registry(self._registry_path)
            resolved: dict[str, ModelSwarmEndpoint] = {}
            for ep_id in request.endpoint_health:
                ep = registry.get(ep_id)
                if ep is not None:
                    resolved[ep_id] = ep
                else:
                    logger.warning(
                        "endpoint_id %r not found in registry — skipping", ep_id
                    )
            return resolved
        return {}

    def handle(self, request: ModelSwarmFanoutRequest) -> ModelSwarmFanoutResult:
        if request.dispatch_mode == EnumDispatchMode.QUEUE:
            return self._handle_queue(request)
        return self._handle_direct(request)

    def _handle_direct(
        self, request: ModelSwarmFanoutRequest
    ) -> ModelSwarmFanoutResult:
        import httpx

        client: ProtocolHttpClient = self._http_client or httpx.Client()
        config = request.config
        endpoint_map = self._resolve_endpoint_map(request)

        endpoint_semaphores: dict[str, threading.Semaphore] = {
            ep_id: threading.Semaphore(config.max_subtasks_per_endpoint)
            for ep_id in endpoint_map
        }

        waves = _compute_waves(request.subtasks)
        total_deadline = time.monotonic() + config.total_run_timeout_seconds
        all_dispatches: list[ModelSwarmDispatch] = []
        failed_ids: set[str] = set()

        wall_start = time.monotonic()

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

            wave_results = self._dispatch_wave(
                wave_num=wave_num,
                subtasks=runnable,
                assignments=request.assignments,
                endpoint_map=endpoint_map,
                endpoint_semaphores=endpoint_semaphores,
                config=config,
                total_deadline=total_deadline,
                client=client,
                fallback_policy_enabled=config.fallback_policy_enabled,
            )
            for d in wave_results:
                all_dispatches.append(d)
                if d.execution_status != EnumExecutionStatus.SUCCEEDED:
                    failed_ids.add(d.subtask_id)

        wall_latency_ms = int((time.monotonic() - wall_start) * 1000)
        sum_latency = sum(d.latency_ms for d in all_dispatches)

        return ModelSwarmFanoutResult(
            dispatches=tuple(all_dispatches),
            wall_latency_ms=wall_latency_ms,
            sum_subtask_latency_ms=sum_latency,
            run_id=request.run_id,
        )

    def _handle_queue(self, request: ModelSwarmFanoutRequest) -> ModelSwarmFanoutResult:
        if self._queue_publisher is None or self._queue_subscriber is None:
            raise ValueError(
                "queue_publisher and queue_subscriber are required for dispatch_mode=queue"
            )

        publish_topics = contract_publish_topics(_CONTRACT_PATH)
        assignment_topic = next(
            t for t in publish_topics if "swarm-subtask-assigned" in t
        )
        subscribe_topics = contract_subscribe_topics(_CONTRACT_PATH)
        completion_topic = next(
            t for t in subscribe_topics if "swarm-subtask-completed" in t
        )

        config = request.config
        waves = _compute_waves(request.subtasks)
        total_deadline = time.monotonic() + config.total_run_timeout_seconds
        all_dispatches: list[ModelSwarmDispatch] = []
        failed_ids: set[str] = set()

        wall_start = time.monotonic()

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

            wave_dispatches = self._dispatch_wave_queue(
                wave_num=wave_num,
                subtasks=runnable,
                request=request,
                assignment_topic=assignment_topic,
                completion_topic=completion_topic,
                total_deadline=total_deadline,
            )
            for d in wave_dispatches:
                all_dispatches.append(d)
                if d.execution_status != EnumExecutionStatus.SUCCEEDED:
                    failed_ids.add(d.subtask_id)

        wall_latency_ms = int((time.monotonic() - wall_start) * 1000)
        sum_latency = sum(d.latency_ms for d in all_dispatches)

        return ModelSwarmFanoutResult(
            dispatches=tuple(all_dispatches),
            wall_latency_ms=wall_latency_ms,
            sum_subtask_latency_ms=sum_latency,
            run_id=request.run_id,
        )

    def _dispatch_wave_queue(
        self,
        wave_num: int,
        subtasks: list[ModelSubtask],
        request: ModelSwarmFanoutRequest,
        assignment_topic: str,
        completion_topic: str,
        total_deadline: float,
    ) -> list[ModelSwarmDispatch]:
        assert self._queue_publisher is not None
        assert self._queue_subscriber is not None

        config = request.config
        endpoint_map = self._resolve_endpoint_map(request)
        published: dict[str, ModelSwarmSubtaskAssignment] = {}

        for subtask in subtasks:
            time_left = total_deadline - time.monotonic()
            if time_left <= 0:
                continue

            endpoint_id = request.assignments.get(subtask.subtask_id, "")
            endpoint = endpoint_map.get(endpoint_id)
            worker_id = request.worker_assignments.get(subtask.subtask_id, "")

            assignment = ModelSwarmSubtaskAssignment(
                run_id=request.run_id,
                subtask_id=subtask.subtask_id,
                worker_id=worker_id,
                description=subtask.description,
                model_id=endpoint.model_id if endpoint else "",
                endpoint_url=endpoint.base_url if endpoint else "",
                timeout_seconds=min(
                    config.per_endpoint_timeout_seconds, int(time_left)
                ),
                correlation_id=request.correlation_id,
            )
            self._queue_publisher.publish(assignment_topic, assignment.model_dump())
            published[subtask.subtask_id] = assignment

        if not published:
            return [
                _immediate_timeout(s, wave_num, "budget_exhausted") for s in subtasks
            ]

        time_left = max(total_deadline - time.monotonic(), 1.0)
        raw_results = self._queue_subscriber.poll(
            completion_topic,
            request.run_id,
            timeout_seconds=time_left,
        )

        result_by_subtask: dict[str, ModelSwarmSubtaskResult] = {}
        for raw in raw_results:
            try:
                r = ModelSwarmSubtaskResult.model_validate(raw)
                result_by_subtask[r.subtask_id] = r
            except Exception:
                pass

        dispatches: list[ModelSwarmDispatch] = []
        for subtask in subtasks:
            published_assignment: ModelSwarmSubtaskAssignment | None = published.get(
                subtask.subtask_id
            )
            if published_assignment is None:
                dispatches.append(
                    _immediate_timeout(subtask, wave_num, "budget_exhausted")
                )
                continue

            result = result_by_subtask.get(subtask.subtask_id)
            endpoint_id = request.assignments.get(subtask.subtask_id, "")
            endpoint = endpoint_map.get(endpoint_id)

            if result is None:
                dispatches.append(
                    ModelSwarmDispatch(
                        subtask_id=subtask.subtask_id,
                        endpoint_id=endpoint_id,
                        model_id=endpoint.model_id if endpoint else "",
                        base_url=endpoint.base_url if endpoint else "",
                        execution_status=EnumExecutionStatus.TIMEOUT,
                        failure_reason="no_result_from_worker",
                        wave=wave_num,
                    )
                )
            else:
                dispatches.append(
                    ModelSwarmDispatch(
                        subtask_id=subtask.subtask_id,
                        endpoint_id=endpoint_id,
                        model_id=result.model_id
                        or (endpoint.model_id if endpoint else ""),
                        base_url=endpoint.base_url if endpoint else "",
                        execution_status=result.execution_status,
                        response_text=result.response_text,
                        failure_reason=result.failure_reason,
                        latency_ms=result.latency_ms,
                        wave=wave_num,
                    )
                )
        return dispatches

    def _dispatch_wave(
        self,
        wave_num: int,
        subtasks: list[ModelSubtask],
        assignments: dict[str, str],
        endpoint_map: dict[str, ModelSwarmEndpoint],
        endpoint_semaphores: dict[str, threading.Semaphore],
        config: ModelSwarmConfig,
        total_deadline: float,
        client: ProtocolHttpClient,
        fallback_policy_enabled: bool,
    ) -> list[ModelSwarmDispatch]:
        worker_count = min(config.max_parallel_subtasks, len(subtasks))
        futures: dict[concurrent.futures.Future[ModelSwarmDispatch], ModelSubtask] = {}

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_count
        ) as executor:
            for subtask in subtasks:
                time_left = total_deadline - time.monotonic()
                if time_left <= 0:
                    futures[
                        executor.submit(
                            _immediate_timeout, subtask, wave_num, "budget_exhausted"
                        )
                    ] = subtask
                    continue

                endpoint_id = assignments.get(subtask.subtask_id, "")
                endpoint = endpoint_map.get(endpoint_id)
                if endpoint is None:
                    futures[
                        executor.submit(_no_endpoint_dispatch, subtask, wave_num)
                    ] = subtask
                    continue

                semaphore = endpoint_semaphores.get(
                    endpoint_id, threading.Semaphore(config.max_subtasks_per_endpoint)
                )
                per_timeout = min(float(config.per_endpoint_timeout_seconds), time_left)
                futures[
                    executor.submit(
                        self._run_subtask,
                        subtask=subtask,
                        endpoint=endpoint,
                        semaphore=semaphore,
                        per_timeout=per_timeout,
                        wave_num=wave_num,
                        client=client,
                        config=config,
                        endpoint_map=endpoint_map,
                        endpoint_semaphores=endpoint_semaphores,
                        assignments=assignments,
                        fallback_policy_enabled=fallback_policy_enabled,
                    )
                ] = subtask

            results: list[ModelSwarmDispatch] = []
            remaining = max(total_deadline - time.monotonic(), 1.0)
            try:
                for future in concurrent.futures.as_completed(
                    futures, timeout=remaining
                ):
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        subtask = futures[future]
                        results.append(
                            ModelSwarmDispatch(
                                subtask_id=subtask.subtask_id,
                                endpoint_id="",
                                model_id="",
                                base_url="",
                                execution_status=EnumExecutionStatus.FAILED,
                                failure_class=type(exc).__name__,
                                failure_reason=str(exc),
                                wave=wave_num,
                            )
                        )
            except concurrent.futures.TimeoutError:
                for future, subtask in futures.items():
                    if not future.done():
                        future.cancel()
                        results.append(
                            ModelSwarmDispatch(
                                subtask_id=subtask.subtask_id,
                                endpoint_id="",
                                model_id="",
                                base_url="",
                                execution_status=EnumExecutionStatus.TIMEOUT,
                                failure_reason="total_run_timeout",
                                wave=wave_num,
                            )
                        )

        return results

    def _run_subtask(
        self,
        subtask: ModelSubtask,
        endpoint: ModelSwarmEndpoint,
        semaphore: threading.Semaphore,
        per_timeout: float,
        wave_num: int,
        client: ProtocolHttpClient,
        config: ModelSwarmConfig,
        endpoint_map: dict[str, ModelSwarmEndpoint],
        endpoint_semaphores: dict[str, threading.Semaphore],
        assignments: dict[str, str],
        fallback_policy_enabled: bool,
    ) -> ModelSwarmDispatch:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        t0 = time.monotonic()

        with semaphore:
            try:
                response = client.post(
                    f"{endpoint.base_url}/chat/completions",
                    json={
                        "model": endpoint.model_id,
                        "messages": [{"role": "user", "content": subtask.description}],
                    },
                    timeout=per_timeout,
                )
                data: dict[str, object] = response.json()
                choices = data.get("choices", [])
                text = ""
                if isinstance(choices, list) and choices:
                    first = choices[0]
                    if isinstance(first, dict):
                        msg = first.get("message", {})
                        if isinstance(msg, dict):
                            text = str(msg.get("content", ""))
                latency_ms = int((time.monotonic() - t0) * 1000)
                completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                return ModelSwarmDispatch(
                    subtask_id=subtask.subtask_id,
                    endpoint_id=endpoint.endpoint_id,
                    model_id=endpoint.model_id,
                    base_url=endpoint.base_url,
                    endpoint_status=endpoint.status,
                    execution_status=EnumExecutionStatus.SUCCEEDED,
                    response_text=text,
                    started_at=started_at,
                    completed_at=completed_at,
                    latency_ms=latency_ms,
                    wave=wave_num,
                )
            except Exception as primary_exc:
                latency_ms = int((time.monotonic() - t0) * 1000)
                completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                failure_class = type(primary_exc).__name__
                failure_reason = str(primary_exc)

                # Retry: try each available endpoint up to max_retries times
                if config.retry_policy_max_retries > 0:
                    for attempt in range(config.retry_policy_max_retries):
                        try:
                            response = client.post(
                                f"{endpoint.base_url}/chat/completions",
                                json={
                                    "model": endpoint.model_id,
                                    "messages": [
                                        {"role": "user", "content": subtask.description}
                                    ],
                                },
                                timeout=per_timeout,
                            )
                            data = response.json()
                            choices = data.get("choices", [])
                            text = ""
                            if isinstance(choices, list) and choices:
                                first = choices[0]
                                if isinstance(first, dict):
                                    msg = first.get("message", {})
                                    if isinstance(msg, dict):
                                        text = str(msg.get("content", ""))
                            latency_ms = int((time.monotonic() - t0) * 1000)
                            completed_at = time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                            )
                            return ModelSwarmDispatch(
                                subtask_id=subtask.subtask_id,
                                endpoint_id=endpoint.endpoint_id,
                                model_id=endpoint.model_id,
                                base_url=endpoint.base_url,
                                endpoint_status=endpoint.status,
                                execution_status=EnumExecutionStatus.SUCCEEDED,
                                response_text=text,
                                retry_count=attempt + 1,
                                started_at=started_at,
                                completed_at=completed_at,
                                latency_ms=latency_ms,
                                wave=wave_num,
                            )
                        except Exception:
                            pass

                # Fallback: try a different endpoint
                if fallback_policy_enabled:
                    primary_id = endpoint.endpoint_id
                    for fb_ep in endpoint_map.values():
                        if fb_ep.endpoint_id == primary_id:
                            continue
                        fb_sem = endpoint_semaphores.get(
                            fb_ep.endpoint_id, threading.Semaphore(1)
                        )
                        try:
                            with fb_sem:
                                response = client.post(
                                    f"{fb_ep.base_url}/chat/completions",
                                    json={
                                        "model": fb_ep.model_id,
                                        "messages": [
                                            {
                                                "role": "user",
                                                "content": subtask.description,
                                            }
                                        ],
                                    },
                                    timeout=per_timeout,
                                )
                                data = response.json()
                                choices = data.get("choices", [])
                                text = ""
                                if isinstance(choices, list) and choices:
                                    first = choices[0]
                                    if isinstance(first, dict):
                                        msg = first.get("message", {})
                                        if isinstance(msg, dict):
                                            text = str(msg.get("content", ""))
                                latency_ms = int((time.monotonic() - t0) * 1000)
                                completed_at = time.strftime(
                                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                )
                                return ModelSwarmDispatch(
                                    subtask_id=subtask.subtask_id,
                                    endpoint_id=endpoint.endpoint_id,
                                    model_id=endpoint.model_id,
                                    base_url=endpoint.base_url,
                                    endpoint_status=endpoint.status,
                                    execution_status=EnumExecutionStatus.SUCCEEDED,
                                    response_text=text,
                                    retry_count=1,
                                    fallback_endpoint_id=fb_ep.endpoint_id,
                                    started_at=started_at,
                                    completed_at=completed_at,
                                    latency_ms=latency_ms,
                                    wave=wave_num,
                                )
                        except Exception:
                            continue

                return ModelSwarmDispatch(
                    subtask_id=subtask.subtask_id,
                    endpoint_id=endpoint.endpoint_id,
                    model_id=endpoint.model_id,
                    base_url=endpoint.base_url,
                    endpoint_status=endpoint.status,
                    execution_status=EnumExecutionStatus.FAILED,
                    failure_class=failure_class,
                    failure_reason=failure_reason,
                    retry_count=config.retry_policy_max_retries,
                    started_at=started_at,
                    completed_at=completed_at,
                    latency_ms=latency_ms,
                    wave=wave_num,
                )


def _no_endpoint_dispatch(subtask: ModelSubtask, wave_num: int) -> ModelSwarmDispatch:
    return ModelSwarmDispatch(
        subtask_id=subtask.subtask_id,
        endpoint_id="",
        model_id="",
        base_url="",
        execution_status=EnumExecutionStatus.FAILED,
        failure_reason="no_endpoint_assigned",
        wave=wave_num,
    )


def _immediate_timeout(
    subtask: ModelSubtask, wave_num: int, reason: str
) -> ModelSwarmDispatch:
    return ModelSwarmDispatch(
        subtask_id=subtask.subtask_id,
        endpoint_id="",
        model_id="",
        base_url="",
        execution_status=EnumExecutionStatus.TIMEOUT,
        failure_reason=reason,
        wave=wave_num,
    )
