#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Proof-of-life script for node_seam_parallel_executor.

Steps:
1. 2-task wave config — independent tasks, assert all_succeeded=True,
   shim_outputs["OutputA"] == "result_from_a", shims_removed=True.
2. Dependency chain — task B depends on task A, confirm ordering.

Related:
    - OMN-8035: Proof of life — run verifier and seam-parallel via onex run
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from uuid import UUID

from omnimarket.nodes.node_seam_parallel_executor.handlers.handler_seam_parallel_executor import (
    HandlerSeamParallelExecutor,
)
from omnimarket.nodes.node_seam_parallel_executor.models.model_seam_task import (
    ModelSeamParallelInput,
    ModelSeamParallelResult,
    ModelSeamTask,
)


def _print_result(label: str, result: ModelSeamParallelResult) -> None:
    print(f"\n{'=' * 60}")
    print(f"CASE: {label}")
    print(f"{'=' * 60}")
    data = {
        "correlation_id": str(result.correlation_id),
        "all_succeeded": result.all_succeeded,
        "shims_removed": result.shims_removed,
        "waves_executed": result.waves_executed,
        "task_results": [
            {
                "task_id": r.task_id,
                "status": r.status,
                "output": r.output,
                "error": r.error,
            }
            for r in result.task_results
        ],
    }
    print(json.dumps(data, indent=2, default=str))


async def _noop_shim_a(
    payload: dict[str, Any], upstream_outputs: dict[str, Any]
) -> str:
    """Shim for task A — returns a fixed string."""
    return "result_from_a"


async def _noop_shim_b(
    payload: dict[str, Any], upstream_outputs: dict[str, Any]
) -> str:
    """Shim for task B — returns a fixed string."""
    return "result_from_b"


async def _downstream_shim(
    payload: dict[str, Any], upstream_outputs: dict[str, Any]
) -> dict[str, Any]:
    """Shim that consumes upstream output from task A."""
    return {
        "received_from_a": upstream_outputs.get("task-a"),
        "my_output": "downstream_result",
    }


async def main() -> int:
    # ------------------------------------------------------------------
    # Case 1: 2 independent tasks in a single wave
    # ------------------------------------------------------------------
    print("\nCASE 1: 2 independent tasks (single wave)")

    executor = HandlerSeamParallelExecutor(
        shim_registry={
            "shim_a": _noop_shim_a,
            "shim_b": _noop_shim_b,
        }
    )

    input_model = ModelSeamParallelInput(
        correlation_id=UUID("00000000-0000-0000-0000-000000000001"),
        tasks=(
            ModelSeamTask(
                task_id="task-a",
                callable_key="shim_a",
                payload={"name": "OutputA"},
            ),
            ModelSeamTask(
                task_id="task-b",
                callable_key="shim_b",
                payload={"name": "OutputB"},
            ),
        ),
        timeout_seconds=10.0,
    )

    result = await executor.handle(input_model)
    _print_result("2 independent tasks", result)

    assert result.all_succeeded is True, (
        f"Expected all_succeeded=True, got {result.all_succeeded}"
    )
    assert result.shims_removed is True, (
        f"Expected shims_removed=True, got {result.shims_removed}"
    )
    assert result.waves_executed == 1, f"Expected 1 wave, got {result.waves_executed}"

    # Build shim_outputs by task_id
    shim_outputs = {r.task_id: r.output for r in result.task_results}
    assert shim_outputs["task-a"] == "result_from_a", (
        f"Expected shim_outputs['task-a'] == 'result_from_a', got {shim_outputs['task-a']!r}"
    )
    assert shim_outputs["task-b"] == "result_from_b", (
        f"Expected shim_outputs['task-b'] == 'result_from_b', got {shim_outputs['task-b']!r}"
    )

    print("ASSERTION: all_succeeded == True  ✓")
    print("ASSERTION: shim_outputs['task-a'] == 'result_from_a'  ✓")
    print("ASSERTION: shim_outputs['task-b'] == 'result_from_b'  ✓")
    print("ASSERTION: shims_removed == True  ✓")
    print("ASSERTION: waves_executed == 1  ✓")

    # ------------------------------------------------------------------
    # Case 2: dependency chain (B depends on A, 2 waves)
    # ------------------------------------------------------------------
    print("\nCASE 2: dependency chain (task-downstream depends on task-a)")

    executor2 = HandlerSeamParallelExecutor(
        shim_registry={
            "shim_a": _noop_shim_a,
            "shim_downstream": _downstream_shim,
        }
    )

    input_model2 = ModelSeamParallelInput(
        correlation_id=UUID("00000000-0000-0000-0000-000000000002"),
        tasks=(
            ModelSeamTask(
                task_id="task-a",
                callable_key="shim_a",
                payload={},
            ),
            ModelSeamTask(
                task_id="task-downstream",
                callable_key="shim_downstream",
                depends_on=("task-a",),
                payload={},
            ),
        ),
        timeout_seconds=10.0,
    )

    result2 = await executor2.handle(input_model2)
    _print_result("dependency chain", result2)

    assert result2.all_succeeded is True
    assert result2.waves_executed == 2, (
        f"Expected 2 waves, got {result2.waves_executed}"
    )

    outputs2 = {r.task_id: r.output for r in result2.task_results}
    assert outputs2["task-a"] == "result_from_a"
    assert isinstance(outputs2["task-downstream"], dict)
    assert outputs2["task-downstream"]["received_from_a"] == "result_from_a"

    print("ASSERTION: all_succeeded == True  ✓")
    print("ASSERTION: waves_executed == 2  ✓")
    print("ASSERTION: downstream received 'result_from_a' from task-a  ✓")

    print("\n" + "=" * 60)
    print("ALL SEAM-PARALLEL PROOF-OF-LIFE ASSERTIONS PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
