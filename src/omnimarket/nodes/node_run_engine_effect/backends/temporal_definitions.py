# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Temporal workflow and activity of the self-hosted overlay.

The workflow takes two JSON strings (the run spec and the settings the overlay supplied) so the history holds exactly
what the caller sent. Every command step is one activity run as a plain process; every wait is a signal wait on a
named condition. Temporal delivers an activity at least once, so the command receives its idempotency key
(``<run_key>/<step name>``) and owns making a repeat delivery a no-op.
"""

from __future__ import annotations

import json
from datetime import timedelta
from functools import partial

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from omnimarket.nodes.node_run_engine_effect.backends.step_runner import (
        SubprocessStepRunner,
    )
    from omnimarket.nodes.node_run_engine_effect.backends.temporal_names import (
        ACTIVITY_NAME,
        SIGNAL_NAME,
        WORKFLOW_NAME,
    )


@activity.defn(name=ACTIVITY_NAME)
async def run_engine_command_step(
    run_key: str, step_json: str, heartbeat_interval_seconds: float
) -> str:
    """Run one command step; the last output line is the step's recorded output."""
    step = json.loads(step_json)
    runner = SubprocessStepRunner(heartbeat_interval_seconds)
    result = await runner.run(
        tuple(step["argv"]),
        run_key,
        step["name"],
        activity.info().attempt,
        heartbeat=lambda: activity.heartbeat(step["name"]),
    )
    if result.exit_code != 0:
        raise RuntimeError(f"step {step['name']} exited {result.exit_code}")
    return result.output


@workflow.defn(name=WORKFLOW_NAME)
class RunEngineWorkflow:
    def __init__(self) -> None:
        self._releases: dict[str, str] = {}

    @workflow.signal(name=SIGNAL_NAME)
    def release(self, payload: str) -> None:
        body = json.loads(payload)
        self._releases.setdefault(body["condition"], body["release_id"])

    def _is_released(self, condition: str) -> bool:
        return condition in self._releases

    @workflow.run
    async def run(self, spec_json: str, settings_json: str) -> str:
        spec = json.loads(spec_json)
        cfg = json.loads(settings_json)
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=cfg["retry_initial_seconds"]),
            backoff_coefficient=1.0,
            maximum_interval=timedelta(seconds=cfg["retry_max_seconds"]),
            maximum_attempts=cfg["retry_max_attempts"],
        )
        for step in spec["steps"]:
            if step["kind"] == "wait_release":
                condition = step["condition"]
                await workflow.wait_condition(partial(self._is_released, condition))
                continue
            await workflow.execute_activity(
                ACTIVITY_NAME,
                args=[
                    spec["run_key"],
                    json.dumps(step),
                    cfg["heartbeat_interval_seconds"],
                ],
                start_to_close_timeout=timedelta(
                    seconds=cfg["command_start_to_close_seconds"]
                ),
                heartbeat_timeout=timedelta(seconds=cfg["heartbeat_timeout_seconds"]),
                retry_policy=retry,
            )
        return "completed"
