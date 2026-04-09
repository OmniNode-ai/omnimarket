# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for deterministic wave-based parallel task execution.

Accepts a task graph with dependencies, topologically sorts into waves,
creates protocol shims, executes each wave via asyncio.gather, and
cleans up shims on completion.

Related:
    - OMN-8032: node_seam_parallel_executor in omnimarket
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Literal, Protocol, runtime_checkable

from omnimarket.nodes.node_seam_parallel_executor.models.model_seam_task import (
    EnumSeamTaskStatus,
    ModelSeamParallelInput,
    ModelSeamParallelResult,
    ModelSeamTask,
    ModelSeamTaskResult,
)

logger = logging.getLogger(__name__)

HandlerType = Literal["NODE_HANDLER", "INFRA_HANDLER", "PROJECTION_HANDLER"]
HandlerCategory = Literal["EFFECT", "COMPUTE", "NONDETERMINISTIC_COMPUTE"]


@runtime_checkable
class ProtocolSeamShim(Protocol):
    """Protocol for pluggable task shim implementations.

    Each shim wraps a callable that executes a single task unit.
    Shims are created before execution and removed after all waves complete.
    """

    async def execute(
        self, payload: dict[str, Any], upstream_outputs: dict[str, Any]
    ) -> Any:
        """Execute the shim with the given payload and upstream task outputs.

        Args:
            payload: Task-specific payload from ModelSeamTask.
            upstream_outputs: Outputs from completed dependency tasks,
                keyed by task_id.

        Returns:
            Arbitrary output from the shim execution.
        """
        ...


class _FunctionShim:
    """Adapts a plain async callable into a ProtocolSeamShim."""

    def __init__(self, fn: Callable[..., Any]) -> None:
        self._fn = fn

    async def execute(
        self, payload: dict[str, Any], upstream_outputs: dict[str, Any]
    ) -> Any:
        return await self._fn(payload=payload, upstream_outputs=upstream_outputs)


def _build_waves(tasks: tuple[ModelSeamTask, ...]) -> list[list[ModelSeamTask]]:
    """Topologically sort tasks into dependency-ordered waves.

    Tasks with no unmet dependencies form wave 0. Tasks whose dependencies
    are all satisfied in prior waves form the next wave, and so on.

    Raises:
        ValueError: If a dependency cycle is detected or a dependency
            references a non-existent task_id.
    """
    seen: set[str] = set()
    for t in tasks:
        if t.task_id in seen:
            msg = f"Duplicate task_id {t.task_id!r} in task list"
            raise ValueError(msg)
        seen.add(t.task_id)

    task_map: dict[str, ModelSeamTask] = {t.task_id: t for t in tasks}

    for task in tasks:
        for dep in task.depends_on:
            if dep not in task_map:
                msg = f"Task {task.task_id!r} depends on unknown task {dep!r}"
                raise ValueError(msg)

    completed: set[str] = set()
    remaining = dict(task_map)
    waves: list[list[ModelSeamTask]] = []

    while remaining:
        wave = [
            t for t in remaining.values() if all(d in completed for d in t.depends_on)
        ]
        if not wave:
            unresolved = list(remaining.keys())
            msg = f"Dependency cycle detected among tasks: {unresolved}"
            raise ValueError(msg)

        wave.sort(key=lambda t: t.task_id)
        waves.append(wave)
        for t in wave:
            completed.add(t.task_id)
            del remaining[t.task_id]

    return waves


class HandlerSeamParallelExecutor:
    """Deterministic wave executor for parallel task execution.

    Creates protocol shims from a registry, sorts tasks into dependency
    waves, executes each wave in parallel via asyncio.gather, and
    captures per-task outputs. Shims are removed on completion.

    Args:
        shim_registry: Mapping from callable_key to ProtocolSeamShim or
            async callable. Plain callables are wrapped in _FunctionShim.
    """

    def __init__(
        self,
        shim_registry: dict[str, ProtocolSeamShim | Callable[..., Any]] | None = None,
    ) -> None:
        self._registry: dict[str, ProtocolSeamShim] = {}
        if shim_registry:
            for key, shim in shim_registry.items():
                if isinstance(shim, ProtocolSeamShim):
                    self._registry[key] = shim
                else:
                    self._registry[key] = _FunctionShim(shim)
        self._active_shims: dict[str, ProtocolSeamShim] = {}

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "EFFECT"

    async def handle(
        self,
        input_model: ModelSeamParallelInput,
    ) -> ModelSeamParallelResult:
        """Execute tasks in dependency-ordered parallel waves.

        Args:
            input_model: Execution input with task graph and timeout.

        Returns:
            ModelSeamParallelResult with per-task results and cleanup status.
        """
        correlation_id = input_model.correlation_id
        tasks = input_model.tasks

        logger.info(
            "Seam parallel executor started (correlation_id=%s, tasks=%d)",
            correlation_id,
            len(tasks),
        )

        if not tasks:
            logger.warning("No tasks provided, returning all_succeeded=False")
            return ModelSeamParallelResult(
                correlation_id=correlation_id,
                all_succeeded=False,
                task_results=(),
                shims_removed=True,
                waves_executed=0,
            )

        # Validate all callable_keys exist in registry
        for task in tasks:
            if task.callable_key not in self._registry:
                msg = (
                    f"Task {task.task_id!r} references unknown "
                    f"callable_key {task.callable_key!r}"
                )
                raise ValueError(msg)

        # Build waves
        waves = _build_waves(tasks)

        # Create shims
        self._active_shims = dict(self._registry.items())

        task_outputs: dict[str, Any] = {}
        all_results: list[ModelSeamTaskResult] = []
        waves_executed = 0

        try:
            for wave in waves:
                waves_executed += 1
                wave_results = await self._execute_wave(
                    wave=wave,
                    task_outputs=task_outputs,
                    global_timeout=input_model.timeout_seconds,
                )
                all_results.extend(wave_results)

                for result in wave_results:
                    if result.status == EnumSeamTaskStatus.COMPLETED:
                        task_outputs[result.task_id] = result.output
        finally:
            self._active_shims.clear()

        all_succeeded = all(
            r.status == EnumSeamTaskStatus.COMPLETED for r in all_results
        )

        logger.info(
            "Seam parallel executor completed (correlation_id=%s, "
            "all_succeeded=%s, waves=%d)",
            correlation_id,
            all_succeeded,
            waves_executed,
        )

        return ModelSeamParallelResult(
            correlation_id=correlation_id,
            all_succeeded=all_succeeded,
            task_results=tuple(all_results),
            shims_removed=True,
            waves_executed=waves_executed,
        )

    async def _execute_wave(
        self,
        wave: list[ModelSeamTask],
        task_outputs: dict[str, Any],
        global_timeout: float,
    ) -> list[ModelSeamTaskResult]:
        """Execute a single wave of independent tasks in parallel."""
        coros = [
            self._execute_task(task, task_outputs, global_timeout) for task in wave
        ]
        return list(await asyncio.gather(*coros))

    async def _execute_task(
        self,
        task: ModelSeamTask,
        task_outputs: dict[str, Any],
        global_timeout: float,
    ) -> ModelSeamTaskResult:
        """Execute a single task via its protocol shim."""
        shim = self._active_shims[task.callable_key]
        timeout = task.timeout_seconds or global_timeout

        upstream_outputs = {
            dep_id: task_outputs[dep_id]
            for dep_id in task.depends_on
            if dep_id in task_outputs
        }

        try:
            output = await asyncio.wait_for(
                shim.execute(payload=task.payload, upstream_outputs=upstream_outputs),
                timeout=timeout,
            )
            return ModelSeamTaskResult(
                task_id=task.task_id,
                status=EnumSeamTaskStatus.COMPLETED,
                output=output,
            )
        except TimeoutError:
            logger.warning("Task %s timed out after %ss", task.task_id, timeout)
            return ModelSeamTaskResult(
                task_id=task.task_id,
                status=EnumSeamTaskStatus.TIMEOUT,
                error=f"Timed out after {timeout}s",
            )
        except Exception as exc:
            logger.error("Task %s failed: %s", task.task_id, exc)
            return ModelSeamTaskResult(
                task_id=task.task_id,
                status=EnumSeamTaskStatus.FAILED,
                error=str(exc),
            )


__all__: list[str] = [
    "HandlerSeamParallelExecutor",
    "ProtocolSeamShim",
]
