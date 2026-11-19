# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Temporal orchestration for the hourly prove-running batch."""

from __future__ import annotations

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from omnimarket.nodes.node_prove_running_workflow.handlers.step_runner import (
        run_step_process,
    )
    from omnimarket.nodes.node_prove_running_workflow.handlers.temporal_prove_running import (
        WORKFLOW_NAME,
    )
    from omnimarket.nodes.node_prove_running_workflow.models import (
        ModelProveRunningBatchPlan,
        ModelProveRunningBatchResult,
        ModelProveRunningBatchStepInput,
        ModelProveRunningHostGroups,
        ModelProveRunningResult,
        ModelProveRunningStartBatch,
    )

BATCH_WORKFLOW_NAME = "ProveRunningBatchWorkflow"
BATCH_ACTIVITY_NAME = "prove_running_batch_step"


@activity.defn(name=BATCH_ACTIVITY_NAME)
async def prove_running_batch_step(argv: list[str], step_input_json: str) -> str:
    """Run a batch step's node command and return its last line of stdout."""
    step_input = ModelProveRunningBatchStepInput.model_validate_json(step_input_json)
    result = await run_step_process(
        tuple(argv),
        step_input_json,
        step_name=step_input.step,
        step_key=step_input.step_key,
        attempt=activity.info().attempt,
        heartbeat=lambda: activity.heartbeat(step_input.step_key),
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"step {step_input.step_key} exited {result.exit_code}: {result.error}"
        )
    return result.output


@workflow.defn(name=BATCH_WORKFLOW_NAME)
class ProveRunningBatchWorkflow:
    """Select and group changes with node commands, then start and await their proofs."""

    @workflow.run
    async def run(self, plan_json: str) -> str:
        plan = ModelProveRunningBatchPlan.model_validate_json(plan_json)
        info = workflow.info()

        async def step(name: str, argv: tuple[str, ...], payload: str = "") -> str:
            step_input = ModelProveRunningBatchStepInput(
                step=name,
                step_key=f"{info.workflow_id}/{name}",
                batch_id=info.workflow_id,
                limit=plan.limit,
                payload=payload,
            )
            return str(
                await workflow.execute_activity(
                    BATCH_ACTIVITY_NAME,
                    args=[list(argv), step_input.model_dump_json()],
                    start_to_close_timeout=timedelta(seconds=plan.step_timeout_seconds),
                    retry_policy=RetryPolicy(maximum_attempts=plan.step_max_attempts),
                )
            )

        read_json = await step("read_not_running", plan.steps.read_not_running)
        batch = ModelProveRunningStartBatch.model_validate_json(read_json)
        grouped = ModelProveRunningHostGroups.model_validate_json(
            await step("group_by_host", plan.steps.group_by_host, read_json)
        )

        started: dict[str, workflow.ChildWorkflowHandle[object, str]] = {}
        already_proven_or_running: list[str] = []
        for group in grouped.groups:
            for child_plan in group.plans:
                child_id = child_plan.change.workflow_id
                try:
                    started[child_id] = await workflow.start_child_workflow(
                        WORKFLOW_NAME,
                        child_plan.model_dump_json(),
                        id=child_id,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                        task_queue=info.task_queue,
                        result_type=str,
                    )
                except WorkflowAlreadyStartedError:
                    already_proven_or_running.append(child_id)

        outcomes = {}
        for child_id, handle in started.items():
            result = ModelProveRunningResult.model_validate_json(await handle)
            outcomes[child_id] = result.outcome.value
        return ModelProveRunningBatchResult(
            batch_id=info.workflow_id,
            read_changes=len(batch.changes),
            hosts={group.host: len(group.plans) for group in grouped.groups},
            started=tuple(started),
            already_proven_or_running=tuple(already_proven_or_running),
            outcomes=outcomes,
        ).model_dump_json()
