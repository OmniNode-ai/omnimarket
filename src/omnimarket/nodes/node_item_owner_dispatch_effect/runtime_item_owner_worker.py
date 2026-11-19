# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The item owner workflow's worker and schedule on a lab host's Temporal server (OMN-25981).

* ``worker``: serve the overlay's task queue (the lab host's temporal worker unit starts it as an ``also_serves`` entry).
* ``schedule``: create or update the overlay's schedule (every ``interval_minutes``, overlap SKIP), optionally trigger.

The step commands, the schedule id, the interval, the task queue and the step bounds are all the overlay's.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

from omnimarket.nodes.node_item_owner_dispatch_effect.handlers.temporal_item_owner import (
    TICK_WORKFLOW_NAME,
    ModelItemOwnerSteps,
    ModelItemOwnerTickPlan,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.overlay import (
    ModelItemOwnerTemporalSettings,
    load_overlay,
)

if TYPE_CHECKING:
    from temporalio.client import Client, Schedule

NODE = "node_item_owner_dispatch_effect"


def _argv(template: tuple[str, ...], registry_root: str) -> tuple[str, ...]:
    """A step's argv with ``{python}`` as this interpreter and ``{registry_root}`` filled."""
    return tuple(
        sys.executable
        if part == "{python}"
        else part.replace("{registry_root}", registry_root)
        for part in template
    )


def build_tick_plan(
    settings: ModelItemOwnerTemporalSettings, registry_root: str
) -> ModelItemOwnerTickPlan:
    return ModelItemOwnerTickPlan(
        steps=ModelItemOwnerSteps(
            plan=_argv(settings.steps.plan, registry_root),
            place=_argv(settings.steps.place, registry_root),
            read=_argv(settings.steps.read, registry_root),
        ),
        child_prefix=settings.child_prefix,
        step_timeout_seconds=settings.step_timeout_seconds,
        step_max_attempts=settings.step_max_attempts,
    )


def build_schedule(
    settings: ModelItemOwnerTemporalSettings, registry_root: str
) -> Schedule:
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
    )

    return Schedule(
        action=ScheduleActionStartWorkflow(
            TICK_WORKFLOW_NAME,
            build_tick_plan(settings, registry_root).model_dump_json(),
            id=settings.schedule_id,
            task_queue=settings.task_queue,
            execution_timeout=timedelta(minutes=settings.interval_minutes),
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(every=timedelta(minutes=settings.interval_minutes))
            ]
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


async def _connect(target_host: str, namespace: str) -> Client:
    from temporalio.client import Client

    return await Client.connect(target_host, namespace=namespace)


async def upsert_schedule(
    client: Client,
    settings: ModelItemOwnerTemporalSettings,
    registry_root: str,
    *,
    trigger: bool,
) -> str:
    from temporalio.client import (
        ScheduleAlreadyRunningError,
        ScheduleUpdate,
        ScheduleUpdateInput,
    )

    schedule = build_schedule(settings, registry_root)

    def update(current: ScheduleUpdateInput) -> ScheduleUpdate:
        return ScheduleUpdate(
            schedule=replace(schedule, state=current.description.schedule.state)
        )

    try:
        handle = await client.create_schedule(settings.schedule_id, schedule)
    except ScheduleAlreadyRunningError:
        handle = client.get_schedule_handle(settings.schedule_id)
        await handle.update(update)
    if trigger:
        await handle.trigger()
    return settings.schedule_id


async def run_worker(target_host: str, namespace: str, task_queue: str) -> None:
    from temporalio.worker import Worker

    from omnimarket.nodes.node_item_owner_dispatch_effect.handlers.temporal_item_owner import (
        ItemOwnerPlaceWorkflow,
        ItemOwnerTickWorkflow,
        item_owner_step,
    )

    client = await _connect(target_host, namespace)
    await Worker(
        client,
        task_queue=task_queue,
        workflows=[ItemOwnerTickWorkflow, ItemOwnerPlaceWorkflow],
        activities=[item_owner_step],
    ).run()


def main(argv: list[str] | None = None) -> int:
    settings = load_overlay().temporal
    parser = argparse.ArgumentParser(prog=f"{NODE}.runtime_item_owner_worker")
    parser.add_argument("verb", choices=("worker", "schedule"))
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--omni-home", dest="registry_root", required=True)
    parser.add_argument(
        "--trigger", action="store_true", help="trigger one run now (schedule only)"
    )
    args = parser.parse_args(argv)
    if args.verb == "worker":
        asyncio.run(run_worker(args.target_host, args.namespace, settings.task_queue))
        return 1

    async def _schedule() -> str:
        client = await _connect(args.target_host, args.namespace)
        return await upsert_schedule(
            client, settings, args.registry_root, trigger=args.trigger
        )

    sys.stdout.write(json.dumps({"schedule_id": asyncio.run(_schedule())}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
