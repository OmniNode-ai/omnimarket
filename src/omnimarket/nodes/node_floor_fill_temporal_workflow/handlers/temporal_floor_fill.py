# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Temporal workflow, its one activity and its Schedule for workflow 2: the floor fill on a schedule.

The Schedule fires every 5 minutes with overlap policy SKIP: a run still in progress at the next fire is skipped,
never queued. The workflow body orders the steps and owns the durable claim wait; it holds no fill logic. Every step
is the one activity ``floor_fill_step``, which runs the step's declared node command as a plain process (the step
runner of workflow 1). The step key is ``<workflow id>/<step name>``, with the reading number on claim reads, so a
node can make a repeat delivery a no-op and a test can count how often each step ran. The zero-placement streak
rides from run to run in the workflow result (Temporal's last completion result for a scheduled workflow).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from omnimarket.nodes.node_floor_fill_temporal_workflow.handlers.step_runner import (
        run_step_process,
    )
    from omnimarket.nodes.node_floor_fill_temporal_workflow.models.model_floor_fill_temporal import (
        ModelFloorFillAlarmAnswer,
        ModelFloorFillClaimAnswer,
        ModelFloorFillPoolAnswer,
        ModelFloorFillRunAnswer,
        ModelFloorFillRunResult,
        ModelFloorFillStepInput,
        ModelFloorFillTemporalPlan,
    )

WORKFLOW_NAME = "FloorFillScheduleWorkflow"
ACTIVITY_NAME = "floor_fill_step"
TASK_QUEUE_DEFAULT = "onex-floor-fill"


class FloorFillStepActivity:
    """The one activity. The worker hands in its environment, so no handler reads the process environment itself."""

    def __init__(self, base_env: Mapping[str, str]) -> None:
        self._base_env = dict(base_env)

    @activity.defn(name=ACTIVITY_NAME)
    async def floor_fill_step(self, argv: list[str], step_input_json: str) -> str:
        """Run one step's node command; its last output line is the step's answer. A non-zero exit fails the attempt."""
        step_input = ModelFloorFillStepInput.model_validate_json(step_input_json)
        result = await run_step_process(
            tuple(argv),
            step_input_json,
            step_name=step_input.step,
            step_key=step_input.step_key,
            attempt=activity.info().attempt,
            base_env=self._base_env,
            heartbeat=lambda: activity.heartbeat(step_input.step_key),
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"step {step_input.step_key} exited {result.exit_code}: {result.error}"
            )
        return result.output


@workflow.defn(name=WORKFLOW_NAME)
class FloorFillScheduleWorkflow:
    """read pool -> run the fill once -> (durable wait, read claims) to the window -> alarm step."""

    @workflow.run
    async def run(self, plan_json: str) -> str:
        plan = ModelFloorFillTemporalPlan.model_validate_json(plan_json)
        wf_id = workflow.info().workflow_id
        prior_streak = 0
        if workflow.has_last_completion_result():
            prior = str(workflow.get_last_completion_result(str))
            prior_streak = ModelFloorFillRunResult.model_validate_json(
                prior
            ).zero_streak

        async def step(name: str, argv: tuple[str, ...], **given: object) -> str:
            step_input = ModelFloorFillStepInput.model_validate(
                {
                    "step": name.split("/", 1)[0],
                    "step_key": f"{wf_id}/{name}",
                    "workflow_id": wf_id,
                    "rule": plan.rule,
                    "prior_zero_streak": prior_streak,
                    **given,
                }
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

        pool = ModelFloorFillPoolAnswer.model_validate_json(
            await step("read_pool", plan.steps.read_pool)
        )
        fill = ModelFloorFillRunAnswer.model_validate_json(
            await step("run_fill", plan.steps.run_fill, pool=pool)
        )
        claims: ModelFloorFillClaimAnswer | None = None
        readings = 0
        if fill.placed_lanes:
            closes = workflow.now() + timedelta(seconds=plan.rule.claim_window_seconds)
            while claims is None or claims.unclaimed:
                left = (closes - workflow.now()).total_seconds()
                if left <= 0:
                    break
                await workflow.sleep(
                    timedelta(seconds=min(plan.claim_poll_seconds, left))
                )
                readings += 1
                claims = ModelFloorFillClaimAnswer.model_validate_json(
                    await step(
                        f"read_claims/{readings}",
                        plan.steps.read_claims,
                        pool=pool,
                        fill=fill,
                        window_closed=workflow.now() >= closes,
                    )
                )
        alarm = ModelFloorFillAlarmAnswer.model_validate_json(
            await step(
                "write_alarm",
                plan.steps.write_alarm,
                pool=pool,
                fill=fill,
                claims=claims,
            )
        )
        return ModelFloorFillRunResult(
            workflow_id=wf_id,
            pool=pool,
            fill=fill,
            claims=claims,
            claim_readings=readings,
            zero_streak=alarm.zero_streak,
            alarms=alarm.alarms,
        ).model_dump_json()
