# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_seam_parallel_executor.

Verifies the deterministic wave executor: parallel execution, dependency
ordering, shim lifecycle, error handling, timeout enforcement, and
edge cases.

Related:
    - OMN-8032: node_seam_parallel_executor in omnimarket
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_seam_parallel_executor.handlers.handler_seam_parallel_executor import (
    HandlerSeamParallelExecutor,
)
from omnimarket.nodes.node_seam_parallel_executor.models.model_seam_task import (
    EnumSeamTaskStatus,
    ModelSeamParallelInput,
    ModelSeamTask,
)


class _EchoShim:
    """Shim that echoes back the payload merged with upstream outputs."""

    async def execute(
        self, payload: dict[str, Any], upstream_outputs: dict[str, Any]
    ) -> dict[str, Any]:
        return {**payload, "upstream": upstream_outputs}


class _FailingShim:
    """Shim that always raises."""

    async def execute(
        self, payload: dict[str, Any], upstream_outputs: dict[str, Any]
    ) -> Any:
        msg = "Simulated task failure"
        raise RuntimeError(msg)


class _SlowShim:
    """Shim that sleeps longer than any reasonable timeout."""

    async def execute(
        self, payload: dict[str, Any], upstream_outputs: dict[str, Any]
    ) -> Any:
        await asyncio.sleep(300)
        return "should not reach"


class _OrderTracker:
    """Tracks execution order across tasks."""

    def __init__(self) -> None:
        self.order: list[str] = []

    def make_shim(self, label: str) -> _TrackingShim:
        return _TrackingShim(label=label, tracker=self)


class _TrackingShim:
    """Shim that records its execution order."""

    def __init__(self, label: str, tracker: _OrderTracker) -> None:
        self._label = label
        self._tracker = tracker

    async def execute(
        self, payload: dict[str, Any], upstream_outputs: dict[str, Any]
    ) -> dict[str, Any]:
        self._tracker.order.append(self._label)
        return {"label": self._label, "upstream": upstream_outputs}


@pytest.mark.unit
class TestSeamParallelExecutorGoldenChain:
    """Golden chain: task graph -> wave execution -> per-task results."""

    async def test_seam_parallel_two_independent_tasks(self) -> None:
        """Two tasks with no shared deps both succeed."""
        handler = HandlerSeamParallelExecutor(shim_registry={"echo": _EchoShim()})

        result = await handler.handle(
            ModelSeamParallelInput(
                correlation_id=uuid4(),
                tasks=(
                    ModelSeamTask(
                        task_id="t1",
                        callable_key="echo",
                        payload={"value": 1},
                    ),
                    ModelSeamTask(
                        task_id="t2",
                        callable_key="echo",
                        payload={"value": 2},
                    ),
                ),
            )
        )

        assert result.all_succeeded is True
        assert len(result.task_results) == 2
        assert result.waves_executed == 1
        assert result.shims_removed is True

        results_by_id = {r.task_id: r for r in result.task_results}
        assert results_by_id["t1"].status == EnumSeamTaskStatus.COMPLETED
        assert results_by_id["t1"].output == {"value": 1, "upstream": {}}
        assert results_by_id["t2"].status == EnumSeamTaskStatus.COMPLETED
        assert results_by_id["t2"].output == {"value": 2, "upstream": {}}

    async def test_seam_parallel_shim_removed_on_completion(self) -> None:
        """Shims are cleaned up after execution completes."""
        handler = HandlerSeamParallelExecutor(shim_registry={"echo": _EchoShim()})

        result = await handler.handle(
            ModelSeamParallelInput(
                correlation_id=uuid4(),
                tasks=(ModelSeamTask(task_id="t1", callable_key="echo"),),
            )
        )

        assert result.shims_removed is True
        # Internal shim state should be cleared
        assert len(handler._active_shims) == 0

    async def test_seam_parallel_fails_gracefully_on_task_error(self) -> None:
        """One task raises, others complete. all_succeeded=False."""
        handler = HandlerSeamParallelExecutor(
            shim_registry={
                "echo": _EchoShim(),
                "fail": _FailingShim(),
            }
        )

        result = await handler.handle(
            ModelSeamParallelInput(
                correlation_id=uuid4(),
                tasks=(
                    ModelSeamTask(task_id="t1", callable_key="echo"),
                    ModelSeamTask(task_id="t2", callable_key="fail"),
                ),
            )
        )

        assert result.all_succeeded is False
        results_by_id = {r.task_id: r for r in result.task_results}
        assert results_by_id["t1"].status == EnumSeamTaskStatus.COMPLETED
        assert results_by_id["t2"].status == EnumSeamTaskStatus.FAILED
        assert "Simulated task failure" in (results_by_id["t2"].error or "")

    async def test_seam_parallel_respects_dependency_order(self) -> None:
        """Task B depends on A, reads A's output via upstream_outputs."""
        tracker = _OrderTracker()

        handler = HandlerSeamParallelExecutor(
            shim_registry={
                "track_a": tracker.make_shim("A"),
                "track_b": tracker.make_shim("B"),
            }
        )

        result = await handler.handle(
            ModelSeamParallelInput(
                correlation_id=uuid4(),
                tasks=(
                    ModelSeamTask(
                        task_id="a",
                        callable_key="track_a",
                        payload={"step": "first"},
                    ),
                    ModelSeamTask(
                        task_id="b",
                        callable_key="track_b",
                        depends_on=("a",),
                        payload={"step": "second"},
                    ),
                ),
            )
        )

        assert result.all_succeeded is True
        assert result.waves_executed == 2

        # A must execute before B
        assert tracker.order == ["A", "B"]

        # B receives A's output as upstream
        results_by_id = {r.task_id: r for r in result.task_results}
        b_output = results_by_id["b"].output
        assert b_output["upstream"]["a"]["label"] == "A"

    async def test_seam_parallel_callable_key_mismatch(self) -> None:
        """Missing callable_key raises ValueError."""
        handler = HandlerSeamParallelExecutor(shim_registry={"echo": _EchoShim()})

        with pytest.raises(ValueError, match="unknown callable_key"):
            await handler.handle(
                ModelSeamParallelInput(
                    correlation_id=uuid4(),
                    tasks=(
                        ModelSeamTask(
                            task_id="t1",
                            callable_key="nonexistent",
                        ),
                    ),
                )
            )

    async def test_seam_parallel_empty_tasks_returns_false(self) -> None:
        """Empty task list returns all_succeeded=False."""
        handler = HandlerSeamParallelExecutor(shim_registry={"echo": _EchoShim()})

        result = await handler.handle(
            ModelSeamParallelInput(
                correlation_id=uuid4(),
                tasks=(),
            )
        )

        assert result.all_succeeded is False
        assert result.waves_executed == 0
        assert result.shims_removed is True

    async def test_seam_parallel_timeout_seconds_enforced(self) -> None:
        """Per-task timeout is enforced via asyncio.wait_for."""
        handler = HandlerSeamParallelExecutor(shim_registry={"slow": _SlowShim()})

        result = await handler.handle(
            ModelSeamParallelInput(
                correlation_id=uuid4(),
                tasks=(
                    ModelSeamTask(
                        task_id="t1",
                        callable_key="slow",
                        timeout_seconds=0.1,
                    ),
                ),
                timeout_seconds=60.0,
            )
        )

        assert result.all_succeeded is False
        assert result.task_results[0].status == EnumSeamTaskStatus.TIMEOUT
        assert "Timed out" in (result.task_results[0].error or "")

    async def test_seam_parallel_dependency_cycle_raises(self) -> None:
        """Circular dependency raises ValueError."""
        handler = HandlerSeamParallelExecutor(shim_registry={"echo": _EchoShim()})

        with pytest.raises(ValueError, match="Dependency cycle"):
            await handler.handle(
                ModelSeamParallelInput(
                    correlation_id=uuid4(),
                    tasks=(
                        ModelSeamTask(
                            task_id="a",
                            callable_key="echo",
                            depends_on=("b",),
                        ),
                        ModelSeamTask(
                            task_id="b",
                            callable_key="echo",
                            depends_on=("a",),
                        ),
                    ),
                )
            )

    async def test_seam_parallel_unknown_dependency_raises(self) -> None:
        """Dependency on non-existent task raises ValueError."""
        handler = HandlerSeamParallelExecutor(shim_registry={"echo": _EchoShim()})

        with pytest.raises(ValueError, match="unknown task"):
            await handler.handle(
                ModelSeamParallelInput(
                    correlation_id=uuid4(),
                    tasks=(
                        ModelSeamTask(
                            task_id="a",
                            callable_key="echo",
                            depends_on=("ghost",),
                        ),
                    ),
                )
            )

    async def test_seam_parallel_three_wave_diamond(self) -> None:
        """Diamond dependency: A -> (B, C) -> D produces 3 waves."""
        tracker = _OrderTracker()

        handler = HandlerSeamParallelExecutor(
            shim_registry={
                "a": tracker.make_shim("A"),
                "b": tracker.make_shim("B"),
                "c": tracker.make_shim("C"),
                "d": tracker.make_shim("D"),
            }
        )

        result = await handler.handle(
            ModelSeamParallelInput(
                correlation_id=uuid4(),
                tasks=(
                    ModelSeamTask(task_id="a", callable_key="a"),
                    ModelSeamTask(task_id="b", callable_key="b", depends_on=("a",)),
                    ModelSeamTask(task_id="c", callable_key="c", depends_on=("a",)),
                    ModelSeamTask(
                        task_id="d",
                        callable_key="d",
                        depends_on=("b", "c"),
                    ),
                ),
            )
        )

        assert result.all_succeeded is True
        assert result.waves_executed == 3
        # A must be first, D must be last
        assert tracker.order[0] == "A"
        assert tracker.order[-1] == "D"
