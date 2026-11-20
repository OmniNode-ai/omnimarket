# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The worktree prune run as a Temporal workflow: per host, one pruner step and one report step.

The workflow body orders the steps, owns the retry limit and decides nothing else. Both steps are the one activity
``worktree_prune_step``, which runs the step's declared node command as a plain process (``step_runner``): the pruner
step runs the host's pruner node, the report step runs this node's own ``report`` command. A host whose pruner fails
after every attempt gets a failed row from the report step and the run goes on to the next host. The workflow id is
the change: ``worktree-prune-ticket-<ticket>`` for a closed ticket, the schedule's own id plus its start time for the
timer. The step key is ``<workflow id>/<host>/<step>``, so a node can make a repeat delivery a no-op.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from omnimarket.nodes.node_worktree_prune_temporal_effect.handlers.step_runner import (
        run_step_process,
    )
    from omnimarket.nodes.node_worktree_prune_temporal_effect.models.model_worktree_prune_temporal import (
        ModelPruneHostRow,
        ModelPruneReportRequest,
        ModelWorktreePrunePlan,
        ModelWorktreePruneRunResult,
    )

WORKFLOW_NAME = "WorktreePruneWorkflow"
STEP_ACTIVITY = "worktree_prune_step"
TASK_QUEUE_DEFAULT = "onex-prove-running"


@activity.defn(name=STEP_ACTIVITY)
async def worktree_prune_step(argv: list[str], step_key: str, stdin: str) -> str:
    """Run one step's node command; its last output line is the answer. A non-zero exit fails the attempt."""
    result = await run_step_process(
        tuple(argv),
        stdin,
        step_key=step_key,
        attempt=activity.info().attempt,
        heartbeat=lambda: activity.heartbeat(step_key),
    )
    if result.exit_code != 0:
        raise RuntimeError(f"step {step_key} exited {result.exit_code}: {result.error}")
    return result.output


@workflow.defn(name=WORKFLOW_NAME)
class WorktreePruneWorkflow:
    @workflow.run
    async def run(self, plan_json: str) -> str:
        plan = ModelWorktreePrunePlan.model_validate_json(plan_json)
        wf_id = workflow.info().workflow_id
        rows: list[ModelPruneHostRow] = []
        for target in plan.hosts:
            pruner_key = f"{wf_id}/{target.host}/prune"
            output: str | None = None
            error: str | None = None
            try:
                output = await workflow.execute_activity(
                    STEP_ACTIVITY,
                    args=[list(target.pruner_argv), pruner_key, ""],
                    task_queue=target.task_queue,
                    start_to_close_timeout=timedelta(
                        seconds=plan.activity_timeout_seconds
                    ),
                    heartbeat_timeout=timedelta(seconds=60),
                    retry_policy=RetryPolicy(
                        maximum_attempts=plan.max_attempts,
                        initial_interval=timedelta(seconds=plan.retry_initial_seconds),
                        backoff_coefficient=2.0,
                    ),
                    result_type=str,
                )
            except ActivityError as exc:
                error = str(exc.cause or exc)[:400]
            request = ModelPruneReportRequest(
                step_key=f"{wf_id}/{target.host}/report",
                host=target.host,
                disk_path=target.disk_path,
                disk_bar_percent=target.disk_bar_percent,
                pruner_output=output,
                error=error,
            )
            raw: str = await workflow.execute_activity(
                STEP_ACTIVITY,
                args=[
                    list(plan.report_argv),
                    request.step_key,
                    request.model_dump_json(),
                ],
                task_queue=target.task_queue,
                start_to_close_timeout=timedelta(seconds=120),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(
                    maximum_attempts=plan.max_attempts,
                    initial_interval=timedelta(seconds=plan.retry_initial_seconds),
                    backoff_coefficient=2.0,
                ),
                result_type=str,
            )
            rows.append(ModelPruneHostRow.model_validate_json(raw))
        return ModelWorktreePruneRunResult(
            workflow_id=wf_id, trigger=plan.trigger, rows=tuple(rows)
        ).model_dump_json()
