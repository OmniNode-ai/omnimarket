# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wave-scheduled parallel fan-out dispatcher for swarm execution."""

from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Protocol

from omnimarket.nodes.node_swarm_fanout_effect.models.enums import EnumExecutionStatus
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
    def __init__(self, http_client: ProtocolHttpClient | None = None) -> None:
        self._http_client = http_client

    def handle(self, request: ModelSwarmFanoutRequest) -> ModelSwarmFanoutResult:
        import httpx

        client: ProtocolHttpClient = self._http_client or httpx.Client()
        config = request.config
        endpoint_map = {ep.endpoint_id: ep for ep in request.endpoints}

        # Per-endpoint semaphores for bounded concurrency
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
        )

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
