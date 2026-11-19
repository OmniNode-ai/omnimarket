# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Temporal workflow and its one activity for workflow 1, prove-running.

The workflow body orders the steps and owns the durable timer, the retry limit and the deadline; it holds no other
logic. Every step is the one activity ``prove_running_step``, which runs the step's declared node command as a plain
process (see ``step_runner``). The step key is ``<workflow id>/<step name>``, with the reading number on read steps,
so a node can make a repeat delivery a no-op and a test can count how often each step ran.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from omnimarket.nodes.node_prove_running_workflow.handlers.step_runner import (
        run_step_process,
    )
    from omnimarket.nodes.node_prove_running_workflow.models.model_prove_running import (
        EnumProveRunningOutcome,
        ModelProveRunningAnswer,
        ModelProveRunningPlan,
        ModelProveRunningResult,
        ModelProveRunningStepInput,
    )

WORKFLOW_NAME = "ProveRunningWorkflow"
ACTIVITY_NAME = "prove_running_step"
TASK_QUEUE_DEFAULT = "onex-prove-running"


@activity.defn(name=ACTIVITY_NAME)
async def prove_running_step(argv: list[str], step_input_json: str) -> str:
    """Run one step's node command; its last output line is the step's answer. A non-zero exit fails the attempt."""
    step_input = ModelProveRunningStepInput.model_validate_json(step_input_json)
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


@workflow.defn(name=WORKFLOW_NAME)
class ProveRunningWorkflow:
    """read status -> install when needed -> (durable wait, read running) up to the limit -> STATUS row or alarm."""

    @workflow.run
    async def run(self, plan_json: str) -> str:
        plan = ModelProveRunningPlan.model_validate_json(plan_json)
        wf_id = workflow.info().workflow_id
        deadline = workflow.now() + timedelta(seconds=plan.deadline_seconds)

        async def step(
            name: str, argv: tuple[str, ...], last: ModelProveRunningAnswer | None
        ) -> str:
            step_input = ModelProveRunningStepInput(
                step=name.split("/", 1)[0],
                step_key=f"{wf_id}/{name}",
                change=plan.change,
                last_reading=last,
                batch_id=plan.batch_id,
            )
            return str(
                await workflow.execute_activity(
                    ACTIVITY_NAME,
                    args=[list(argv), step_input.model_dump_json()],
                    start_to_close_timeout=timedelta(seconds=plan.step_timeout_seconds),
                    heartbeat_timeout=timedelta(seconds=60),
                    retry_policy=RetryPolicy(
                        maximum_attempts=plan.step_max_attempts,
                        initial_interval=timedelta(seconds=5),
                        backoff_coefficient=2.0,
                    ),
                )
            )

        status = ModelProveRunningAnswer.model_validate_json(
            await step("read_status", plan.steps.read_status, None)
        )
        installed_here = not status.installed
        if installed_here:
            await step("install", plan.steps.install, status)

        last: ModelProveRunningAnswer | None = None
        readings = 0
        while readings < plan.max_readings and workflow.now() < deadline:
            await workflow.sleep(timedelta(seconds=plan.wait_seconds))
            readings += 1
            last = ModelProveRunningAnswer.model_validate_json(
                await step(f"read_running/{readings}", plan.steps.read_running, last)
            )
            if last.running:
                await step("write_status", plan.steps.write_status, last)
                return self._result(
                    wf_id,
                    EnumProveRunningOutcome.PROVED,
                    readings,
                    installed_here,
                    last,
                )

        await step("write_alarm", plan.steps.write_alarm, last or status)
        return self._result(
            wf_id,
            EnumProveRunningOutcome.ALARM,
            readings,
            installed_here,
            last or status,
        )

    @staticmethod
    def _result(
        wf_id: str,
        outcome: EnumProveRunningOutcome,
        readings: int,
        installed_here: bool,
        last: ModelProveRunningAnswer | None,
    ) -> str:
        return ModelProveRunningResult(
            workflow_id=wf_id,
            outcome=outcome,
            readings=readings,
            installed_by_workflow=installed_here,
            last_reading=last,
        ).model_dump_json()
