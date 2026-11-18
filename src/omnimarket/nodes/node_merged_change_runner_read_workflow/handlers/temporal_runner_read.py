# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Temporal workflow and its one activity for the merged-change runner read.

The workflow orders the steps and owns the retry limit; it holds no reading logic. Every step is the one activity
``runner_read_step``, which runs a declared node command as a plain process with its input on stdin and answers its
stdout: ``lines`` lists the declared lines, ``read_line`` is ``node_merged_change_reading_effect`` for one line (one
activity per declared runner), and ``publish`` hands every outcome to the integration publish. A line whose activity
fails every attempt is carried as unread with the error as its cause. Temporal records each completed activity, so a
worker that dies mid-read resumes at the first unfinished line.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from pydantic import TypeAdapter

    from omnimarket.models.model_merged_change_reading import ModelMcrReading
    from omnimarket.nodes.node_merged_change_runner_read_workflow.models.model_runner_read import (
        ModelRunnerLineOutcome,
        ModelRunnerReadPlan,
        ModelRunnerReadPublishInput,
        ModelRunnerReadWindow,
        ModelRunnerSelectedLine,
    )

WORKFLOW_NAME = "MergedChangeRunnerReadWorkflow"
ACTIVITY_NAME = "runner_read_step"


@activity.defn(name=ACTIVITY_NAME)
async def runner_read_step(argv: list[str], stdin_text: str) -> str:
    """Run one node command; a non-zero exit fails the attempt, and Temporal retries it."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    activity.heartbeat(argv[-1])
    try:
        out, err = await proc.communicate(stdin_text.encode("utf-8"))
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv[-2:])} exited {proc.returncode}: {err.decode('utf-8', 'replace').strip()[-300:]}"
        )
    return out.decode("utf-8")


def _cause(exc: BaseException) -> str:
    inner = exc.cause if isinstance(exc, ActivityError) and exc.cause else exc
    return f"{type(inner).__name__}: {inner}"[:300]


@workflow.defn(name=WORKFLOW_NAME)
class MergedChangeRunnerReadWorkflow:
    """lines -> one read_line activity per declared line (bounded concurrency) -> publish."""

    @workflow.run
    async def run(self, plan_json: str) -> str:
        plan = ModelRunnerReadPlan.model_validate_json(plan_json)
        started = workflow.now()
        retry = RetryPolicy(
            maximum_attempts=plan.step_max_attempts,
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
        )
        timeout = timedelta(seconds=plan.step_timeout_seconds)

        async def run_step(argv: list[str], stdin_text: str, activity_id: str) -> str:
            return str(
                await workflow.execute_activity(
                    ACTIVITY_NAME,
                    args=[argv, stdin_text],
                    activity_id=activity_id,
                    start_to_close_timeout=timeout,
                    heartbeat_timeout=timeout,
                    retry_policy=retry,
                )
            )

        if plan.previous_build_at is None:
            keys = TypeAdapter(list[str]).validate_json(
                await run_step(list(plan.steps.lines), "", "lines")
            )
            selected = []
            for key in keys:
                repo, _, number = key.rpartition("#")
                selected.append(ModelRunnerSelectedLine(repo=repo, number=int(number)))
        else:
            window = ModelRunnerReadWindow(
                previous_build_at=plan.previous_build_at, as_of=started
            )
            selected = TypeAdapter(list[ModelRunnerSelectedLine]).validate_json(
                await run_step(
                    [*plan.steps.lines, "--window"], window.model_dump_json(), "lines"
                )
            )

        async def read_one(line: ModelRunnerSelectedLine) -> ModelRunnerLineOutcome:
            cause = line.unread_cause
            if not cause:
                try:
                    change = (
                        f"{line.key}={line.merge_sha}" if line.merge_sha else line.key
                    )
                    answer = await run_step(
                        [*plan.steps.read_line, "--change", change],
                        "",
                        f"read/{line.key}",
                    )
                    readings = json.loads(answer)["reading"]["readings"]
                    reading = ModelMcrReading.model_validate(readings[0])
                    if (reading.repo, reading.number) != (line.repo, line.number):
                        raise ValueError("the reader answered for a different change")
                    if line.merge_sha and reading.merge_sha != line.merge_sha:
                        raise ValueError("the reader answered for a different merge")
                    return ModelRunnerLineOutcome(
                        repo=line.repo,
                        number=line.number,
                        reading=reading,
                        read_at=workflow.now(),
                    )
                except (
                    ActivityError,
                    ValueError,
                    KeyError,
                    IndexError,
                    TypeError,
                ) as exc:
                    cause = _cause(exc)
            return ModelRunnerLineOutcome(
                repo=line.repo,
                number=line.number,
                unread_cause=cause,
                read_at=workflow.now(),
            )

        outcomes: list[ModelRunnerLineOutcome] = []
        for start in range(0, len(selected), plan.line_concurrency):
            chunk = selected[start : start + plan.line_concurrency]
            outcomes.extend(await asyncio.gather(*(read_one(line) for line in chunk)))
        read_seconds = (workflow.now() - started).total_seconds()
        payload = ModelRunnerReadPublishInput(
            as_of=started,
            run_id=workflow.info().workflow_id,
            read_seconds=read_seconds,
            outcomes=tuple(outcomes),
            publish=plan.publish,
            windowed=plan.previous_build_at is not None,
            merge_facts={line.key: line.merge for line in selected}
            if plan.previous_build_at is not None
            else {},
        )
        return await run_step(
            list(plan.steps.publish), payload.model_dump_json(), "publish"
        )


__all__ = [
    "ACTIVITY_NAME",
    "WORKFLOW_NAME",
    "MergedChangeRunnerReadWorkflow",
    "runner_read_step",
]
