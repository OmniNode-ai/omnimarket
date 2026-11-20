# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The item owner workflow on the lab host's Temporal worker (OMN-25981).

Every step is this node's own command run as a plain process (the prove-running step runner): ``plan`` folds the items
and names the due owners, ``place`` places one owner lane and writes the owner onto the item, ``read`` is the unowned
reading. The tick workflow starts one child workflow per due owner whose workflow id is the owner's reservation key
(item, revision, generation), with the reuse policy that allows a new start only after a failed one: a rerun of the same
tick, a second tick or a second worker asking for the same key gets ``WorkflowAlreadyStartedError`` and places nothing.
That is the engine's workflow-id uniqueness; no lease or store of ours is added.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity, workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import (
    ApplicationError,
    ChildWorkflowError,
    WorkflowAlreadyStartedError,
)

with workflow.unsafe.imports_passed_through():
    from omnimarket.nodes.node_item_owner_dispatch_effect.models import (
        EnumOwnerSkip,
        ModelOwnerDue,
        ModelOwnerPlaced,
        ModelOwnerPlan,
    )
    from omnimarket.nodes.node_prove_running_workflow.handlers.step_runner import (
        run_step_process,
    )

TICK_WORKFLOW_NAME = "ItemOwnerTickWorkflow"
PLACE_WORKFLOW_NAME = "ItemOwnerPlaceWorkflow"
ACTIVITY_NAME = "item_owner_step"
#: A placement that did not start ends its child workflow failed, so the next tick may start the same key again.
RETRY_SKIPS = frozenset(
    {EnumOwnerSkip.BRIEF_CHECK_FAILED, EnumOwnerSkip.PLACEMENT_FAILED}
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelItemOwnerSteps(_Frozen):
    plan: tuple[str, ...] = Field(min_length=1)
    place: tuple[str, ...] = Field(min_length=1)
    read: tuple[str, ...] = Field(min_length=1)


class ModelItemOwnerTickPlan(_Frozen):
    steps: ModelItemOwnerSteps
    child_prefix: str = Field(min_length=1)
    step_timeout_seconds: int = Field(ge=1)
    step_max_attempts: int = Field(ge=1)


class ModelItemOwnerStepInput(_Frozen):
    argv: tuple[str, ...]
    step: str
    step_key: str
    stdin: str = ""
    #: Exit codes that are answers, not failures (the reading's FINDING and UNREAD).
    answer_codes: tuple[int, ...] = (0,)


class ModelItemOwnerStepAnswer(_Frozen):
    exit_code: int
    output: str


class ModelItemOwnerPlaceInput(_Frozen):
    plan: ModelItemOwnerTickPlan
    due: ModelOwnerDue
    tick_id: str


class ModelItemOwnerTickResult(_Frozen):
    workflow_id: str
    run_id: str
    due: int
    placed: tuple[ModelOwnerPlaced, ...]
    already_started: tuple[str, ...]
    failed: tuple[str, ...]
    reading_exit: int
    reading: str


@activity.defn(name=ACTIVITY_NAME)
async def item_owner_step(step_json: str) -> str:
    """Run one step's node command; an exit code outside ``answer_codes`` fails the attempt with the command's text."""
    step = ModelItemOwnerStepInput.model_validate_json(step_json)
    result = await run_step_process(
        step.argv,
        step.stdin,
        step_name=step.step,
        step_key=step.step_key,
        attempt=activity.info().attempt,
        heartbeat=lambda: activity.heartbeat(step.step_key),
    )
    if result.exit_code not in step.answer_codes:
        raise RuntimeError(
            f"step {step.step_key} exited {result.exit_code}: {result.output} {result.error}".strip()
        )
    return ModelItemOwnerStepAnswer(
        exit_code=result.exit_code, output=result.output
    ).model_dump_json()


async def _step(
    plan: ModelItemOwnerTickPlan,
    name: str,
    argv: tuple[str, ...],
    *,
    stdin: str = "",
    answer_codes: tuple[int, ...] = (0,),
) -> ModelItemOwnerStepAnswer:
    info = workflow.info()
    step = ModelItemOwnerStepInput(
        argv=argv,
        step=name,
        step_key=f"{info.workflow_id}/{info.run_id}/{name}",
        stdin=stdin,
        answer_codes=answer_codes,
    )
    out = await workflow.execute_activity(
        ACTIVITY_NAME,
        step.model_dump_json(),
        start_to_close_timeout=timedelta(seconds=plan.step_timeout_seconds),
        heartbeat_timeout=timedelta(seconds=60),
        retry_policy=RetryPolicy(
            maximum_attempts=plan.step_max_attempts,
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
        ),
    )
    return ModelItemOwnerStepAnswer.model_validate_json(str(out))


@workflow.defn(name=PLACE_WORKFLOW_NAME)
class ItemOwnerPlaceWorkflow:
    """One owner binding: its workflow id is the reservation key, so it runs at most once unless it failed."""

    @workflow.run
    async def run(self, input_json: str) -> str:
        place = ModelItemOwnerPlaceInput.model_validate_json(input_json)
        argv = (
            *place.plan.steps.place,
            "--run-id",
            f"{place.tick_id} {workflow.info().workflow_id}",
        )
        answer = await _step(
            place.plan, "place", argv, stdin=place.due.model_dump_json()
        )
        placed = ModelOwnerPlaced.model_validate_json(answer.output)
        if not placed.started and placed.skip in RETRY_SKIPS:
            raise ApplicationError(
                f"{placed.skip.value if placed.skip else 'not-started'}: {placed.detail}",
                non_retryable=True,
            )
        return placed.model_dump_json()


@workflow.defn(name=TICK_WORKFLOW_NAME)
class ItemOwnerTickWorkflow:
    """plan -> one child per due owner (id = reservation key) -> the unowned reading."""

    @workflow.run
    async def run(self, plan_json: str) -> str:
        plan = ModelItemOwnerTickPlan.model_validate_json(plan_json)
        info = workflow.info()
        tick_id = f"{info.workflow_id}/{info.run_id}"
        owners = ModelOwnerPlan.model_validate_json(
            (await _step(plan, "plan", plan.steps.plan)).output
        )
        placed: list[ModelOwnerPlaced] = []
        already: list[str] = []
        failed: list[str] = []
        for due in owners.due:
            child_id = f"{plan.child_prefix}{due.reservation_key}"
            try:
                handle = await workflow.start_child_workflow(
                    PLACE_WORKFLOW_NAME,
                    ModelItemOwnerPlaceInput(
                        plan=plan, due=due, tick_id=tick_id
                    ).model_dump_json(),
                    id=child_id,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                )
            except WorkflowAlreadyStartedError:
                already.append(child_id)
                continue
            try:
                placed.append(ModelOwnerPlaced.model_validate_json(str(await handle)))
            except ChildWorkflowError as exc:
                failed.append(f"{child_id}: {exc.cause or exc}")
        reading = await _step(plan, "read", plan.steps.read, answer_codes=(0, 1, 2))
        return ModelItemOwnerTickResult(
            workflow_id=info.workflow_id,
            run_id=info.run_id,
            due=len(owners.due),
            placed=tuple(placed),
            already_started=tuple(already),
            failed=tuple(failed),
            reading_exit=reading.exit_code,
            reading=reading.output,
        ).model_dump_json()


__all__ = [
    "ACTIVITY_NAME",
    "PLACE_WORKFLOW_NAME",
    "TICK_WORKFLOW_NAME",
    "ItemOwnerPlaceWorkflow",
    "ItemOwnerTickWorkflow",
    "ModelItemOwnerSteps",
    "ModelItemOwnerTickPlan",
    "ModelItemOwnerTickResult",
    "item_owner_step",
]
