# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Temporal orchestration for the hourly prove-running batch.

Every NOT RUNNING line the integration pass published is read and ends in exactly one result row: proven (carrying its
reading), blocked (carrying its owner) or next-step (carrying the one next step). The change's own workflow writes the
row of a change it proves or alarms on; the batch writes the row of every other line (an earlier batch already started
it, its merge sha was unread, or its workflow failed). Those rows are written by ONE step at the end of the pass, so a
pass of hundreds of answered lines is one row process and one bus append, not one of each per line. No step starts an
agent: every step is a node command.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ChildWorkflowError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from omnimarket.nodes.node_prove_running_workflow.handlers.handler_prove_running_rows import (
        alarm_result_kind,
    )
    from omnimarket.nodes.node_prove_running_workflow.handlers.step_runner import (
        run_step_process,
    )
    from omnimarket.nodes.node_prove_running_workflow.handlers.temporal_prove_running import (
        WORKFLOW_NAME,
    )
    from omnimarket.nodes.node_prove_running_workflow.models import (
        EnumProveRunningLineCase,
        EnumProveRunningOutcome,
        EnumProveRunningResultKind,
        ModelProveRunningBatchPlan,
        ModelProveRunningBatchResult,
        ModelProveRunningBatchStepInput,
        ModelProveRunningChange,
        ModelProveRunningEarlier,
        ModelProveRunningHostGroups,
        ModelProveRunningLinePayload,
        ModelProveRunningLinesPayload,
        ModelProveRunningLinesWritten,
        ModelProveRunningResult,
        ModelProveRunningStartBatch,
        ModelProveRunningUnresolvedLine,
    )

BATCH_WORKFLOW_NAME = "ProveRunningBatchWorkflow"
BATCH_ACTIVITY_NAME = "prove_running_batch_step"
EARLIER_ACTIVITY_NAME = "prove_running_earlier"
EARLIER_TIMEOUT_SECONDS = 120


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


@activity.defn(name=EARLIER_ACTIVITY_NAME)
async def prove_running_earlier(workflow_id: str) -> str:
    """Where the workflow an earlier batch started for this change stands: its status and, once ended, its result."""
    handle = activity.client().get_workflow_handle(workflow_id)
    description = await handle.describe()
    status = description.status.name if description.status else "UNKNOWN"
    result: ModelProveRunningResult | None = None
    if status == "COMPLETED":
        try:
            result = ModelProveRunningResult.model_validate_json(await handle.result())
        except ValueError:
            result = None
    return ModelProveRunningEarlier(
        workflow_id=workflow_id, status=status, result=result
    ).model_dump_json()


def _unresolved_payload(
    line: ModelProveRunningUnresolvedLine,
) -> ModelProveRunningLinePayload:
    """The payload of a line whose merge sha was unread, so no workflow could start for it."""
    return ModelProveRunningLinePayload(
        case=EnumProveRunningLineCase.UNRESOLVED,
        key=line.key,
        target=line.target,
        owner=line.owner,
        next_step=line.next_step,
        detail=line.reason,
    )


@workflow.defn(name=BATCH_WORKFLOW_NAME)
class ProveRunningBatchWorkflow:
    """Read every NOT RUNNING line, start one proof per change, and end every line in one result row."""

    @workflow.run
    async def run(self, plan_json: str) -> str:
        plan = ModelProveRunningBatchPlan.model_validate_json(plan_json)
        info = workflow.info()

        async def step(
            name: str, argv: tuple[str, ...], payload: str = "", written_at: str = ""
        ) -> str:
            step_input = ModelProveRunningBatchStepInput(
                step=name.split("/", 1)[0],
                step_key=f"{info.workflow_id}/{name}",
                batch_id=info.workflow_id,
                payload=payload,
                written_at=written_at,
            )
            return str(
                await workflow.execute_activity(
                    BATCH_ACTIVITY_NAME,
                    args=[list(argv), step_input.model_dump_json()],
                    start_to_close_timeout=timedelta(seconds=plan.step_timeout_seconds),
                    retry_policy=RetryPolicy(maximum_attempts=plan.step_max_attempts),
                )
            )

        async def answer_already_started(
            change: ModelProveRunningChange,
        ) -> ModelProveRunningLinePayload:
            earlier = ModelProveRunningEarlier.model_validate_json(
                await workflow.execute_activity(
                    EARLIER_ACTIVITY_NAME,
                    args=[change.workflow_id],
                    start_to_close_timeout=timedelta(seconds=EARLIER_TIMEOUT_SECONDS),
                    retry_policy=RetryPolicy(maximum_attempts=plan.step_max_attempts),
                )
            )
            return ModelProveRunningLinePayload(
                case=EnumProveRunningLineCase.ALREADY_STARTED,
                key=change.key,
                target=change.target,
                owner=change.owner,
                next_step=change.next_step,
                workflow_id=change.workflow_id,
                merge_sha=change.merge_sha,
                earlier=earlier,
            )

        async def finish(
            change: ModelProveRunningChange,
            handle: workflow.ChildWorkflowHandle[object, str],
            outcomes: dict[str, str],
        ) -> ModelProveRunningLinePayload | EnumProveRunningResultKind:
            try:
                result = ModelProveRunningResult.model_validate_json(await handle)
            except ChildWorkflowError as exc:
                return ModelProveRunningLinePayload(
                    case=EnumProveRunningLineCase.CHILD_FAILED,
                    key=change.key,
                    target=change.target,
                    owner=change.owner,
                    next_step=change.next_step,
                    workflow_id=change.workflow_id,
                    merge_sha=change.merge_sha,
                    detail=f"{type(exc.cause).__name__ if exc.cause else 'failed'}: {exc.cause or exc}",
                )
            outcomes[change.workflow_id] = result.outcome.value
            return (
                EnumProveRunningResultKind.PROVEN
                if result.outcome is EnumProveRunningOutcome.PROVED
                else alarm_result_kind(change)
            )

        read_json = await step("read_not_running", plan.steps.read_not_running)
        batch = ModelProveRunningStartBatch.model_validate_json(read_json)
        grouped = ModelProveRunningHostGroups.model_validate_json(
            await step("group_by_host", plan.steps.group_by_host, read_json)
        )

        started: dict[str, workflow.ChildWorkflowHandle[object, str]] = {}
        changes: dict[str, ModelProveRunningChange] = {}
        already_proven_or_running: list[str] = []
        for group in grouped.groups:
            for child_plan in group.plans:
                child_id = child_plan.change.workflow_id
                changes[child_id] = child_plan.change
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

        outcomes: dict[str, str] = {}
        earlier_answers = await asyncio.gather(
            *[answer_already_started(changes[c]) for c in already_proven_or_running],
        )
        child_answers = await asyncio.gather(
            *[finish(changes[c], handle, outcomes) for c, handle in started.items()],
        )
        answered: list[ModelProveRunningLinePayload | EnumProveRunningResultKind] = [
            *earlier_answers,
            *[_unresolved_payload(line) for line in batch.unresolved],
            *child_answers,
        ]
        # A line the batch answers itself is one payload; every such row goes out in one step. The payloads keep the
        # order they were gathered in: earlier starts, unresolved lines, then the new starts whose workflow failed.
        to_write = tuple(
            a for a in answered if isinstance(a, ModelProveRunningLinePayload)
        )
        written_kinds: list[EnumProveRunningResultKind] = []
        if to_write:
            written_kinds = list(
                ModelProveRunningLinesWritten.model_validate_json(
                    await step(
                        "write_line_result",
                        plan.steps.write_line_result,
                        ModelProveRunningLinesPayload(lines=to_write).model_dump_json(),
                        written_at=workflow.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    )
                ).results
            )
        kinds = [
            written_kinds.pop(0) if isinstance(a, ModelProveRunningLinePayload) else a
            for a in answered
        ]
        order = [
            *(changes[c].key for c in already_proven_or_running),
            *(line.key for line in batch.unresolved),
            *(changes[c].key for c in started),
        ]
        results = {key: kind.value for key, kind in zip(order, kinds, strict=True)}
        return ModelProveRunningBatchResult(
            batch_id=info.workflow_id,
            read_changes=len(batch.changes),
            hosts={group.host: len(group.plans) for group in grouped.groups},
            started=tuple(started),
            already_proven_or_running=tuple(already_proven_or_running),
            outcomes=outcomes,
            lines_read=batch.lines_read,
            results_written=len(results),
            results=results,
        ).model_dump_json()
