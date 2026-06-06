# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerMultiAgentOrchestrator — Multi-agent workflow coordination orchestrator.

ONEX node type: ORCHESTRATOR — impure, effectful, fan-out/fan-in.

Bounded production slice: dry-run returns a deterministic dispatch plan and live
agent execution requires an injected dispatcher adapter.

Workflow modes (per multi_agent SKILL.md):
  parallel_debug   — Phase 1: requirements gathering → Phase 2: N parallel debug agents
                     → Phase 3: reconcile results → terminal event.
  parallel_build   — Phase 1: requirements gathering → Phase 2: N parallel build agents
                     → Phase 3: quality validation → Phase 4: refactor (max 3 attempts)
                     → Phase 5: approval gate → Phase 6: commit+PR.
  sequential_review — load plan → for each task: dispatch subagent → dispatch reviewer
                      → apply feedback → mark complete → final review.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_multi_agent_orchestrator.models.model_multi_agent import (
    EnumAgentResultStatus,
    EnumConflictClass,
    EnumWorkflowType,
    ModelAgentResult,
    ModelAgentTask,
    ModelConflictField,
    ModelMultiAgentResult,
    ModelReconciliation,
)

# ---------------------------------------------------------------------------
# Request model (lives here so contract.yaml input_model path is canonical)
# ---------------------------------------------------------------------------


class ModelMultiAgentRequest(BaseModel):
    """Input envelope for the multi-agent orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_type: EnumWorkflowType = Field(
        description="Workflow mode: parallel_debug, parallel_build, or sequential_review.",
    )
    tasks: list[ModelAgentTask] = Field(
        description=(
            "Tasks to dispatch. For parallel modes, tasks with no `depends_on` "
            "are dispatched concurrently. Sequential tasks respect dependency order."
        ),
    )
    concurrency: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of agents running concurrently (parallel modes only).",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When true, log dispatch decisions without spawning real agents. "
            "Useful for verifying task decomposition before live execution."
        ),
    )
    correlation_id: str | None = Field(
        default=None,
        description="Upstream correlation ID for event tracing.",
    )


class ProtocolAgentTaskDispatcher(Protocol):
    """Adapter boundary for live sub-agent dispatch."""

    def dispatch_task(
        self,
        task: ModelAgentTask,
        *,
        workflow_type: EnumWorkflowType,
        correlation_id: str | None,
    ) -> ModelAgentResult | dict[str, object]: ...


class HandlerMultiAgentOrchestrator:
    """ORCHESTRATOR — multi-agent workflow fan-out/fan-in coordinator.

    Dry-run never spawns agents. Live execution is available only through the
    injected ``ProtocolAgentTaskDispatcher`` boundary.
    """

    def __init__(self, dispatcher: ProtocolAgentTaskDispatcher | None = None) -> None:
        self._dispatcher = dispatcher

    def handle(self, request: ModelMultiAgentRequest) -> ModelMultiAgentResult:
        """Execute the multi-agent workflow.

        Raises:
            RuntimeError: when live execution is requested without a dispatcher.
        """
        ordered_tasks = _dependency_order(request.tasks)
        if request.dry_run:
            results = [
                ModelAgentResult(
                    task_id=task.task_id,
                    status=EnumAgentResultStatus.SKIPPED,
                    summary=f"dry_run: would dispatch {request.workflow_type.value}",
                    files_changed=[],
                    findings=[_task_finding(task)],
                )
                for task in ordered_tasks
            ]
        else:
            if self._dispatcher is None:
                raise RuntimeError("dispatcher adapter required when dry_run is false")
            results = [
                _coerce_agent_result(
                    self._dispatcher.dispatch_task(
                        task,
                        workflow_type=request.workflow_type,
                        correlation_id=request.correlation_id,
                    )
                )
                for task in ordered_tasks
            ]

        reconciliation = (
            _reconcile(results)
            if request.workflow_type
            in {
                EnumWorkflowType.PARALLEL_BUILD,
                EnumWorkflowType.PARALLEL_DEBUG,
            }
            else None
        )
        succeeded = sum(
            1 for result in results if result.status is EnumAgentResultStatus.SUCCESS
        )
        failed = sum(
            1
            for result in results
            if result.status
            in {EnumAgentResultStatus.FAILURE, EnumAgentResultStatus.TIMEOUT}
        )
        skipped = sum(
            1 for result in results if result.status is EnumAgentResultStatus.SKIPPED
        )
        return ModelMultiAgentResult(
            workflow_type=request.workflow_type,
            agent_results=results,
            reconciliation=reconciliation,
            succeeded_count=succeeded,
            failed_count=failed,
            skipped_count=skipped,
            total_files_changed=_dedupe_files(results),
            aggregated_findings=_aggregate_findings(results),
            approval_required=(
                reconciliation.requires_approval
                if reconciliation is not None
                else False
            ),
        )


def _dependency_order(tasks: list[ModelAgentTask]) -> list[ModelAgentTask]:
    by_id = {task.task_id: task for task in tasks}
    missing = sorted(
        {
            dependency
            for task in tasks
            for dependency in task.depends_on
            if dependency not in by_id
        }
    )
    if missing:
        raise ValueError(f"Unknown task dependencies: {', '.join(missing)}")

    ordered: list[ModelAgentTask] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task: ModelAgentTask) -> None:
        if task.task_id in visited:
            return
        if task.task_id in visiting:
            raise ValueError(f"Task dependency cycle detected at {task.task_id}")
        visiting.add(task.task_id)
        for dependency_id in task.depends_on:
            visit(by_id[dependency_id])
        visiting.remove(task.task_id)
        visited.add(task.task_id)
        ordered.append(task)

    for task in tasks:
        visit(task)
    return ordered


def _coerce_agent_result(
    value: ModelAgentResult | dict[str, object],
) -> ModelAgentResult:
    if isinstance(value, ModelAgentResult):
        return value
    return ModelAgentResult.model_validate(value)


def _task_finding(task: ModelAgentTask) -> str:
    scope = ", ".join(task.scope) if task.scope else "unspecified scope"
    return f"{task.task_id}: {task.description} ({scope})"


def _dedupe_files(results: list[ModelAgentResult]) -> list[str]:
    return sorted({path for result in results for path in result.files_changed})


def _aggregate_findings(results: list[ModelAgentResult]) -> list[str]:
    return [
        f"{result.task_id}: {finding}"
        for result in results
        for finding in result.findings
    ]


def _reconcile(results: list[ModelAgentResult]) -> ModelReconciliation:
    writers: dict[str, list[str]] = defaultdict(list)
    for result in results:
        if result.status is not EnumAgentResultStatus.SUCCESS:
            continue
        for path in result.files_changed:
            writers[path].append(result.task_id)

    approval_fields = [
        ModelConflictField(
            field_path=f"files_changed.{path}",
            conflict_class=EnumConflictClass.REQUIRES_APPROVAL,
            competing_values=dict.fromkeys(task_ids, path),
        )
        for path, task_ids in sorted(writers.items())
        if len(task_ids) > 1
    ]
    merged_values = {
        f"files_changed.{path}": task_ids[0]
        for path, task_ids in sorted(writers.items())
        if len(task_ids) == 1
    }
    return ModelReconciliation(
        requires_approval=bool(approval_fields),
        merged_values=merged_values,
        approval_required_fields=approval_fields,
        optional_review_fields=[],
    )


__all__ = [
    "HandlerMultiAgentOrchestrator",
    "ModelMultiAgentRequest",
    "ProtocolAgentTaskDispatcher",
]
