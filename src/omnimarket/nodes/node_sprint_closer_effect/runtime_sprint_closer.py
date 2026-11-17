# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI of the sprint closer effect node.

worker         run the Temporal worker for the closer's task queue on the shared Temporal server
schedule       create (or update) the every-2-hours schedule
seen-running   start one closer run for a change the integration reader marked SEEN RUNNING (signal-with-start)
run-once       run the closer in this process, ticket by ticket, with the same handler (the one-time triage)
read           print a finished workflow run's per-ticket decision list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import cast

import yaml

from omnimarket.nodes.node_sprint_closer_effect.handlers.sprint_closer_sources import (
    live_sources_from_env,
)
from omnimarket.nodes.node_sprint_closer_effect.models.model_sprint_closer import (
    ModelSprintCloserPlan,
    ModelSprintCloserRunResult,
)

CONTRACT = Path(__file__).with_name("contract.yaml")


def load_config() -> dict[str, object]:
    data = cast(dict[str, object], yaml.safe_load(CONTRACT.read_text()))
    return cast(dict[str, object], data["sprint_closer"])


def build_plan(cfg: dict[str, object], *, dry_run: bool, trigger: str, actor: str | None,
               tickets: list[str] | None) -> ModelSprintCloserPlan:
    return ModelSprintCloserPlan(
        project_id=str(cfg["project_id"]),
        owner_assignees=tuple(cast(list[str], cfg["owner_assignees"])),
        actor=actor or str(cfg["actor"]),
        dry_run=dry_run,
        trigger=trigger,
        ticket_ids=tuple(tickets or ()),
        activity_timeout_seconds=int(cast(int, cfg["activity_timeout_seconds"])),
        activity_max_attempts=int(cast(int, cfg["activity_max_attempts"])),
    )


async def _client(target: str, namespace: str):  # type: ignore[no-untyped-def]
    from temporalio.client import Client

    return await Client.connect(target, namespace=namespace)


async def run_worker(target: str, namespace: str, queue: str, omni_home: Path) -> None:
    from temporalio.worker import Worker

    from omnimarket.nodes.node_sprint_closer_effect.handlers.temporal_sprint_closer import (
        SprintCloserActivities,
        SprintCloserWorkflow,
    )

    acts = SprintCloserActivities(live_sources_from_env(omni_home))
    client = await _client(target, namespace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        await Worker(
            client, task_queue=queue, workflows=[SprintCloserWorkflow],
            activities=[acts.list_tickets, acts.run_ticket], activity_executor=pool,
        ).run()


async def create_schedule(target: str, namespace: str, queue: str, plan: ModelSprintCloserPlan,
                          schedule_id: str, hours: int) -> str:
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleIntervalSpec,
        SchedulePolicy,
        ScheduleSpec,
        ScheduleUpdate,
        ScheduleUpdateInput,
    )
    from temporalio.client import ScheduleOverlapPolicy

    from omnimarket.nodes.node_sprint_closer_effect.handlers.temporal_sprint_closer import WORKFLOW_NAME

    client = await _client(target, namespace)
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            WORKFLOW_NAME, plan.model_dump_json(), id=f"{schedule_id}-run", task_queue=queue
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(hours=hours))]),
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


async def start_seen_running(target: str, namespace: str, queue: str, plan: ModelSprintCloserPlan,
                             change: str) -> str:
    from temporalio.common import WorkflowIDReusePolicy

    from omnimarket.nodes.node_sprint_closer_effect.handlers.temporal_sprint_closer import (
        SEEN_RUNNING_SIGNAL,
        WORKFLOW_NAME,
    )

    client = await _client(target, namespace)
    wf_id = f"sprint-closer-seen-running-{change}"
    await client.start_workflow(
        WORKFLOW_NAME, plan.model_dump_json(), id=wf_id, task_queue=queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        start_signal=SEEN_RUNNING_SIGNAL, start_signal_args=[change],
    )
    return wf_id


async def read_run(target: str, namespace: str, workflow_id: str) -> str:
    client = await _client(target, namespace)
    return cast(str, await client.get_workflow_handle(workflow_id).result())


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(prog="node_sprint_closer_effect")
    sub = ap.add_subparsers(dest="verb", required=True)
    for verb in ("worker", "schedule", "seen-running", "run-once", "read"):
        p = sub.add_parser(verb)
        p.add_argument("--target-host", default=os.environ.get("ONEX_TEMPORAL_ADDRESS", str(cfg["target_host"])))
        p.add_argument("--namespace", default="default")
        p.add_argument("--task-queue", default=str(cfg["task_queue"]))
        p.add_argument("--omni-home", default=os.environ.get("OMNI_HOME", "."))
        p.add_argument("--live", action="store_true", help="write comments and states (default: dry run)")
        p.add_argument("--actor", default=None)
        p.add_argument("--ticket", action="append", default=None)
        p.add_argument("--change", default=None)
        p.add_argument("--workflow-id", default=None)
        p.add_argument("--journal", default=None, help="run-once: append one JSON line per ticket here")
    args = ap.parse_args(argv)
    queue, target = args.task_queue, args.target_host
    if args.verb == "worker":
        asyncio.run(run_worker(target, args.namespace, queue, Path(args.omni_home)))
        return 0
    if args.verb == "read":
        print(asyncio.run(read_run(target, args.namespace, args.workflow_id)))
        return 0
    trigger = {"schedule": "schedule", "seen-running": f"seen_running:{args.change}"}.get(args.verb, "manual")
    plan = build_plan(cfg, dry_run=not args.live, trigger=trigger, actor=args.actor, tickets=args.ticket)
    if args.verb == "schedule":
        print(asyncio.run(create_schedule(target, args.namespace, queue, plan, str(cfg["schedule_id"]),
                                          int(cast(int, cfg["interval_hours"])))))
        return 0
    if args.verb == "seen-running":
        if not args.change:
            ap.error("seen-running needs --change <repo>#<n>")
        print(asyncio.run(start_seen_running(target, args.namespace, queue, plan, args.change)))
        return 0
    sources = live_sources_from_env(Path(args.omni_home))
    outcomes = []
    for tid in sources.list_ticket_ids(plan):
        try:
            outcome = sources.run_ticket(plan, tid)
        except Exception as exc:  # one ticket's failure is reported, the others still run
            print(json.dumps({"ticket_id": tid, "error": str(exc)[:500]}), file=sys.stderr)
            continue
        outcomes.append(outcome)
        line = outcome.model_dump_json()
        print(line, flush=True)
        if args.journal:
            with open(args.journal, "a") as fh:
                fh.write(line + "\n")
    print(ModelSprintCloserRunResult(workflow_id="run-once", trigger="manual", outcomes=tuple(outcomes))
          .model_dump_json(), file=sys.stderr)
    return 0
