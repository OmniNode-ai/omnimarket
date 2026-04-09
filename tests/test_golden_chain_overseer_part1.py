# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain integration test — overseer verifier + seam-parallel executor.

Verifies the two nodes work together end-to-end:
1. Create ModelSeamTask instances, register shim factory, run HandlerSeamParallelExecutor
2. Pipe the output through HandlerOverseerVerifier
3. Assert both PASS and ESCALATE paths

Related:
    - OMN-8034: Task 9: Golden chain integration test (verifier + seam-parallel together)
    - OMN-8031: node_overseer_verifier in omnimarket
    - OMN-8032: node_seam_parallel_executor in omnimarket
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier import (
    HandlerOverseerVerifier,
)
from omnimarket.nodes.node_overseer_verifier.models.model_verifier_request import (
    ModelVerifierRequest,
)
from omnimarket.nodes.node_seam_parallel_executor.handlers.handler_seam_parallel_executor import (
    HandlerSeamParallelExecutor,
    ProtocolSeamShim,
)
from omnimarket.nodes.node_seam_parallel_executor.models.model_seam_task import (
    EnumSeamTaskStatus,
    ModelSeamParallelInput,
    ModelSeamTask,
)

# ---------------------------------------------------------------------------
# Shim factory helpers
# ---------------------------------------------------------------------------


class _InterfaceXShim:
    """Mock shim for InterfaceX — returns a deterministic result envelope."""

    async def execute(
        self, payload: dict[str, Any], upstream_outputs: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "task_id": payload.get("task_id", "unknown"),
            "status": "running",
            "domain": payload.get("domain", "build"),
            "node_id": payload.get("node_id", "node_build_loop"),
            "confidence": payload.get("confidence", 0.9),
            "cost_so_far": payload.get("cost_so_far", 0.05),
            "schema_version": "1.0",
            "allowed_actions": ["dispatch", "complete"],
            "upstream": upstream_outputs,
        }


def _make_shim_factory() -> dict[str, ProtocolSeamShim]:
    """Return a shim registry keyed by InterfaceX."""
    return {"interface_x": _InterfaceXShim()}


def _result_to_verifier_request(result_output: dict[str, Any]) -> ModelVerifierRequest:
    """Convert seam executor task output to a ModelVerifierRequest."""
    return ModelVerifierRequest(
        task_id=result_output.get("task_id", "unknown"),
        status=result_output.get("status", "running"),
        domain=result_output.get("domain", "build"),
        node_id=result_output.get("node_id", "node_build_loop"),
        confidence=result_output.get("confidence"),
        cost_so_far=result_output.get("cost_so_far"),
        allowed_actions=result_output.get("allowed_actions", []),
        schema_version=result_output.get("schema_version", "1.0"),
    )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGoldenChainOverseerPart1:
    """End-to-end: seam-parallel executor → overseer verifier."""

    async def test_two_tasks_with_dependency_pipe_to_verifier_pass(self) -> None:
        """task_a runs, task_b depends on task_a; both succeed; verifier returns PASS.

        Step-by-step:
        1. Create two ModelSeamTask instances: task_a (no deps), task_b (depends on task_a)
        2. Register shim factory for InterfaceX
        3. Run HandlerSeamParallelExecutor with both tasks
        4. Assert all_succeeded=True, shims_removed=True
        5. Take output from task_b, run through HandlerOverseerVerifier
        6. Assert verdict=PASS
        """
        # --- Step 1: create tasks ---
        task_a = ModelSeamTask(
            task_id="task_a",
            callable_key="interface_x",
            payload={
                "task_id": "task_a",
                "domain": "build",
                "node_id": "node_build_loop",
                "confidence": 0.9,
                "cost_so_far": 0.02,
            },
        )
        task_b = ModelSeamTask(
            task_id="task_b",
            callable_key="interface_x",
            depends_on=("task_a",),
            payload={
                "task_id": "task_b",
                "domain": "build",
                "node_id": "node_build_loop",
                "confidence": 0.85,
                "cost_so_far": 0.05,
            },
        )

        # --- Step 2+3: register shim factory and run executor ---
        executor = HandlerSeamParallelExecutor(shim_registry=_make_shim_factory())
        exec_result = await executor.handle(
            ModelSeamParallelInput(
                correlation_id=uuid4(),
                tasks=(task_a, task_b),
            )
        )

        # --- Step 4: assert executor result ---
        assert exec_result.all_succeeded is True
        assert exec_result.shims_removed is True
        assert exec_result.waves_executed == 2

        results_by_id = {r.task_id: r for r in exec_result.task_results}
        assert results_by_id["task_a"].status == EnumSeamTaskStatus.COMPLETED
        assert results_by_id["task_b"].status == EnumSeamTaskStatus.COMPLETED

        # task_b output should include upstream reference to task_a
        task_b_output = results_by_id["task_b"].output
        assert isinstance(task_b_output, dict)
        assert "task_a" in task_b_output["upstream"]

        # --- Step 5+6: pipe task_b output through verifier ---
        verifier = HandlerOverseerVerifier()
        verifier_result = verifier.verify(_result_to_verifier_request(task_b_output))

        assert verifier_result["verdict"] == "PASS"

    async def test_verifier_escalates_on_invalid_cost_from_executor_output(
        self,
    ) -> None:
        """Manually construct an envelope with cost_so_far=-1.0 and verify ESCALATE.

        Step-by-step:
        1. Create a synthetic task output (simulating what executor would produce)
           with cost_so_far=-1.0 — an invariant violation
        2. Run through HandlerOverseerVerifier
        3. Assert verdict=ESCALATE, failure_class contains invariant_violation evidence
        """
        # Synthetic envelope that represents a malformed executor output
        synthetic_output = {
            "task_id": "task_b",
            "status": "running",
            "domain": "build",
            "node_id": "node_build_loop",
            "confidence": 0.9,
            "cost_so_far": -1.0,  # invariant violation
            "schema_version": "1.0",
            "allowed_actions": ["dispatch", "complete"],
        }

        verifier = HandlerOverseerVerifier()
        verifier_result = verifier.verify(_result_to_verifier_request(synthetic_output))

        assert verifier_result["verdict"] == "ESCALATE"
        # failure_class reflects invariant violation (DATA_INTEGRITY)
        assert verifier_result["failure_class"] == "DATA_INTEGRITY"

        # Confirm the invariant check failed with INVARIANT_VIOLATION message
        checks = {c["name"]: c for c in verifier_result["checks"]}  # type: ignore[union-attr]
        assert checks["invariant_preservation"]["passed"] is False
        assert "INVARIANT_VIOLATION" in checks["invariant_preservation"]["message"]

    async def test_full_pipeline_two_independent_tasks_both_pass_verifier(
        self,
    ) -> None:
        """Two independent tasks both run and both pass verifier independently.

        Validates that each task's output independently satisfies the verifier.
        """
        task_1 = ModelSeamTask(
            task_id="t1",
            callable_key="interface_x",
            payload={
                "task_id": "t1",
                "domain": "review",
                "node_id": "node_local_review",
                "confidence": 0.95,
                "cost_so_far": 0.01,
            },
        )
        task_2 = ModelSeamTask(
            task_id="t2",
            callable_key="interface_x",
            payload={
                "task_id": "t2",
                "domain": "review",
                "node_id": "node_local_review",
                "confidence": 0.80,
                "cost_so_far": 0.03,
            },
        )

        executor = HandlerSeamParallelExecutor(shim_registry=_make_shim_factory())
        exec_result = await executor.handle(
            ModelSeamParallelInput(
                correlation_id=uuid4(),
                tasks=(task_1, task_2),
            )
        )

        assert exec_result.all_succeeded is True
        assert exec_result.waves_executed == 1  # both run in one wave (no deps)

        verifier = HandlerOverseerVerifier()
        for task_result in exec_result.task_results:
            assert task_result.status == EnumSeamTaskStatus.COMPLETED
            assert isinstance(task_result.output, dict)
            verdict = verifier.verify(_result_to_verifier_request(task_result.output))
            assert verdict["verdict"] == "PASS", (
                f"Verifier failed for {task_result.task_id}: {verdict['summary']}"
            )
