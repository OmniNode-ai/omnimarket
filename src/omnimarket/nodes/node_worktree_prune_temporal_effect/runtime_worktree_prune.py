# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI of the worktree prune workflow node.

worker         run the Temporal worker for the prune task queue on the shared Temporal server
schedule       create (or update) the recurring prune schedule
ticket-closed  start one prune run for a closed ticket (workflow id worktree-prune-ticket-<ticket>)
report         the report step: read one ModelPruneReportRequest on stdin, print the host row
read           print a finished workflow run's result
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import yaml

from omnimarket.nodes.node_worktree_prune_temporal_effect.handlers.handler_prune_report import (
    HandlerPruneReport,
)
from omnimarket.nodes.node_worktree_prune_temporal_effect.models.model_worktree_prune_temporal import (
    ModelPruneHostTarget,
    ModelPruneReportRequest,
    ModelWorktreePrunePlan,
)

CONTRACT = Path(__file__).with_name("contract.yaml")
NODE_MODULE = "omnimarket.nodes.node_worktree_prune_temporal_effect"


def load_config() -> dict[str, object]:
    data = cast(dict[str, object], yaml.safe_load(CONTRACT.read_text()))
    return cast(dict[str, object], data["worktree_prune"])


def build_plan(
    cfg: dict[str, object],
    *,
    trigger: str,
    ticket: str | None,
    host: str,
    task_queue: str,
    pruner_argv: list[str],
    disk_path: str,
    disk_bar_percent: float,
    python: str,
) -> ModelWorktreePrunePlan:
    return ModelWorktreePrunePlan.model_validate(
        {
            "trigger": trigger,
            "ticket": ticket,
            "hosts": [
                ModelPruneHostTarget(
                    host=host,
                    task_queue=task_queue,
                    pruner_argv=tuple(pruner_argv),
                    disk_path=disk_path,
                    disk_bar_percent=disk_bar_percent,
                )
            ],
            "max_attempts": int(cast(int, cfg["max_attempts"])),
            "retry_initial_seconds": int(cast(int, cfg["retry_initial_seconds"])),
            "activity_timeout_seconds": int(cast(int, cfg["activity_timeout_seconds"])),
            "report_argv": [python, "-m", NODE_MODULE, "report"],
        }
    )


def first_fire_offset(
    every: timedelta, first_in: timedelta, now: datetime
) -> timedelta:
    """The interval offset that makes the first fire ``first_in`` from ``now`` (fires are offset + k * every)."""
    target = now + first_in
    return timedelta(seconds=int(target.timestamp()) % int(every.total_seconds()))


async def _client(target: str, namespace: str):  # type: ignore[no-untyped-def]
    from temporalio.client import Client

    return await Client.connect(target, namespace=namespace)


async def run_worker(target: str, namespace: str, queue: str) -> None:
    from temporalio.worker import Worker

    from omnimarket.nodes.node_worktree_prune_temporal_effect.handlers.temporal_worktree_prune import (
        WorktreePruneWorkflow,
        worktree_prune_step,
    )

    client = await _client(target, namespace)
    await Worker(
        client,
        task_queue=queue,
        workflows=[WorktreePruneWorkflow],
        activities=[worktree_prune_step],
    ).run()


async def create_schedule(
    target: str,
    namespace: str,
    queue: str,
    plan: ModelWorktreePrunePlan,
    schedule_id: str,
    every: timedelta,
    first_in: timedelta | None,
) -> str:
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleIntervalSpec,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
        ScheduleUpdate,
        ScheduleUpdateInput,
    )

    from omnimarket.nodes.node_worktree_prune_temporal_effect.handlers.temporal_worktree_prune import (
        WORKFLOW_NAME,
    )

    offset = (
        first_fire_offset(every, first_in, datetime.now(UTC))
        if first_in is not None
        else timedelta(0)
    )
    client = await _client(target, namespace)
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            WORKFLOW_NAME,
            plan.model_dump_json(),
            id=f"{schedule_id}-run",
            task_queue=queue,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every, offset=offset)]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )
    try:
        await client.create_schedule(schedule_id, schedule)
        return "created"
    except ScheduleAlreadyRunningError:

        def _update(_: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(schedule=schedule)

        await client.get_schedule_handle(schedule_id).update(_update)
        return "updated"


async def start_ticket_closed(
    target: str, namespace: str, queue: str, plan: ModelWorktreePrunePlan, ticket: str
) -> str:
    from temporalio.common import WorkflowIDReusePolicy

    from omnimarket.nodes.node_worktree_prune_temporal_effect.handlers.temporal_worktree_prune import (
        WORKFLOW_NAME,
    )

    client = await _client(target, namespace)
    wf_id = f"worktree-prune-ticket-{ticket}"
    await client.start_workflow(
        WORKFLOW_NAME,
        plan.model_dump_json(),
        id=wf_id,
        task_queue=queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )
    return wf_id


async def read_run(target: str, namespace: str, workflow_id: str) -> str:
    client = await _client(target, namespace)
    return cast(str, await client.get_workflow_handle(workflow_id).result())


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(prog="node_worktree_prune_temporal_effect")
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("report")
    for verb in ("worker", "schedule", "ticket-closed", "read"):
        p = sub.add_parser(verb)
        p.add_argument("--target-host", default=str(cfg["target_host"]))
        p.add_argument("--namespace", default="default")
        p.add_argument("--task-queue", default=str(cfg["task_queue"]))
        p.add_argument("--host", default=None)
        p.add_argument(
            "--pruner-argv",
            default=None,
            help="JSON list: the host's pruner node command (it prints its result as the last line)",
        )
        p.add_argument("--disk-path", default=None)
        p.add_argument(
            "--disk-bar-percent",
            type=float,
            default=float(cast(float, cfg["disk_bar_percent"])),
        )
        p.add_argument("--ticket", default=None)
        p.add_argument("--workflow-id", default=None)
        p.add_argument("--first-in-minutes", type=int, default=None)
    args = ap.parse_args(argv)
    if args.verb == "report":
        request = ModelPruneReportRequest.model_validate_json(sys.stdin.read())
        sys.stdout.write(HandlerPruneReport().handle(request).model_dump_json() + "\n")
        return 0
    queue, target = args.task_queue, args.target_host
    if args.verb == "worker":
        asyncio.run(run_worker(target, args.namespace, queue))
        return 0
    if args.verb == "read":
        sys.stdout.write(
            asyncio.run(read_run(target, args.namespace, args.workflow_id)) + "\n"
        )
        return 0
    if not (args.host and args.pruner_argv and args.disk_path):
        ap.error(f"{args.verb} needs --host, --pruner-argv and --disk-path")
    plan = build_plan(
        cfg,
        trigger="schedule" if args.verb == "schedule" else "ticket_closed",
        ticket=args.ticket,
        host=args.host,
        task_queue=queue,
        pruner_argv=json.loads(args.pruner_argv),
        disk_path=args.disk_path,
        disk_bar_percent=args.disk_bar_percent,
        python=sys.executable,
    )
    if args.verb == "schedule":
        first = (
            timedelta(minutes=args.first_in_minutes)
            if args.first_in_minutes is not None
            else None
        )
        sys.stdout.write(
            asyncio.run(
                create_schedule(
                    target,
                    args.namespace,
                    queue,
                    plan,
                    str(cfg["schedule_id"]),
                    timedelta(hours=int(cast(int, cfg["interval_hours"]))),
                    first,
                )
            )
            + "\n"
        )
        return 0
    if not args.ticket:
        ap.error("ticket-closed needs --ticket OMN-n")
    sys.stdout.write(
        asyncio.run(
            start_ticket_closed(target, args.namespace, queue, plan, args.ticket)
        )
        + "\n"
    )
    return 0
