# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_merged_change_runner_read_workflow <verb>``: the node's declared CLI.

Verbs:

* ``worker``: run the Temporal worker of the runner read against the contract's server (``--target-host``,
  ``--namespace`` override it). ``--probe`` checks the imports and returns without a connection.
* ``schedule``: create the Temporal schedule of the full read, or update it to the contract's interval and plan.
* ``start``: start one full read now (``--wait`` prints its result; ``--no-publish`` reads without publishing).
* ``lines``: a step command. Prints the keys of every line the runner overlay declares, as a JSON list.
* ``publish``: a step command. Reads one ``ModelRunnerReadPublishInput`` on stdin, reads each change's merge from its
  canonical clone, builds the integration reading batch, pipes it to ``node_integration_reading_publish_effect``,
  appends the run record and prints it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.handler_runner_reading_integration import (
    HandlerRunnerReadingIntegration,
)
from omnimarket.nodes.node_merged_change_runner_read_workflow.models.model_runner_read import (
    ModelMergeFacts,
    ModelRunnerDeclaredLines,
    ModelRunnerIntegrationConfig,
    ModelRunnerIntegrationRequest,
    ModelRunnerReadPlan,
    ModelRunnerReadPublishInput,
    ModelRunnerReadSteps,
)

NODE = "node_merged_change_runner_read_workflow"


def contract_config() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text)["runner_read"])


def _now() -> datetime:
    return datetime.now(UTC)


def _argv(template: list[str]) -> tuple[str, ...]:
    return tuple(sys.executable if part == "{python}" else part for part in template)


def build_plan(cfg: dict[str, Any], *, publish: bool = True) -> ModelRunnerReadPlan:
    steps = {name: _argv(list(argv)) for name, argv in cfg["steps"].items()}
    return ModelRunnerReadPlan(
        steps=ModelRunnerReadSteps(**steps),
        line_concurrency=int(cfg["line_concurrency"]),
        step_timeout_seconds=int(cfg["step_timeout_seconds"]),
        step_max_attempts=int(cfg["step_max_attempts"]),
        publish=publish,
    )


def declared_lines(
    cfg: dict[str, Any], environ: Mapping[str, str]
) -> ModelRunnerDeclaredLines:
    overlay = cfg["runners_overlay"]
    return load_structured_overlay(
        overlay["overlay_id"],
        ModelRunnerDeclaredLines,
        environ,
        file_env=overlay["overlay_file_env"],
    )


def cmd_lines(cfg: dict[str, Any]) -> int:
    lines = declared_lines(cfg, os.environ)
    sys.stdout.write(json.dumps([line.key for line in lines.runners]) + "\n")
    return 0


def merge_facts(home: Path, repo: str, sha: str, timeout: int) -> ModelMergeFacts:
    """The merge commit's subject and commit time from the canonical clone; empty when either cannot be read."""
    if not sha:
        return ModelMergeFacts()
    try:
        proc = subprocess.run(
            ["git", "-C", str(home / repo), "log", "-1", "--format=%cI%x00%s", sha],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ModelMergeFacts()
    stamp, _, subject = proc.stdout.strip().partition("\x00")
    if proc.returncode != 0 or not stamp:
        return ModelMergeFacts()
    return ModelMergeFacts(
        title=subject, merged_at=datetime.fromisoformat(stamp).astimezone(UTC)
    )


def build_request(
    payload: ModelRunnerReadPublishInput,
    cfg: dict[str, Any],
    environ: Mapping[str, str],
    home: Path,
    host: str,
) -> ModelRunnerIntegrationRequest:
    integration = ModelRunnerIntegrationConfig.model_validate(cfg["integration"])
    lines = {line.key: line for line in declared_lines(cfg, environ).runners}
    facts = {
        o.key: merge_facts(
            home, o.repo, o.reading.merge_sha, int(cfg["git_timeout_seconds"])
        )
        for o in payload.outcomes
        if o.reading is not None
    }
    units = {
        key: integration.target_unit_template.format(unit=line.unit, host=line.host)
        for key, line in lines.items()
        if line.unit and line.host
    }
    return ModelRunnerIntegrationRequest(
        as_of=payload.as_of,
        origin=str(cfg["origin_template"]).format(host=host, run_id=payload.run_id),
        outcomes=payload.outcomes,
        merge_facts=facts,
        unit_targets=units,
        config=integration,
    )


def cmd_publish(cfg: dict[str, Any]) -> int:
    payload = ModelRunnerReadPublishInput.model_validate_json(sys.stdin.read())
    home = Path(os.environ.get(str(cfg["home_env"]), "").strip() or ".")
    host = os.environ.get("ONEX_LEDGER_HOST_NAME", "") or socket.gethostname()
    result = HandlerRunnerReadingIntegration().handle(
        build_request(payload, cfg, os.environ, home, host)
    )
    published: dict[str, Any] = {"published": False, "detail": "publish off"}
    if payload.publish:
        proc = subprocess.run(
            [sys.executable, "-m", str(cfg["publish_module"]), "--home", str(home)],
            input=result.batch.model_dump_json(),
            capture_output=True,
            text=True,
            timeout=int(cfg["publish_timeout_seconds"]),
            check=False,
        )
        if proc.returncode != 0:
            sys.stderr.write(
                f"integration publish exited {proc.returncode}: {proc.stderr.strip()[-300:]}\n"
            )
            return proc.returncode
        published = {"published": True, "detail": proc.stdout.strip()[-300:]}
    record = {
        "run_id": payload.run_id,
        "origin": result.batch.origin,
        "as_of": payload.as_of.isoformat(),
        "read_seconds": round(payload.read_seconds, 3),
        "counts": result.counts.model_dump(),
        "lines_unread_by_activity": [
            o.key for o in payload.outcomes if o.reading is None
        ],
        **published,
    }
    path = home / str(cfg["run_record_relpath"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    sys.stdout.write(json.dumps(record) + "\n")
    return 0


async def connect(target_host: str, namespace: str) -> Any:
    from temporalio.client import Client

    return await Client.connect(target_host, namespace=namespace)


async def run_worker(target_host: str, namespace: str, task_queue: str) -> None:
    from temporalio.worker import Worker

    from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.temporal_runner_read import (
        MergedChangeRunnerReadWorkflow,
        runner_read_step,
    )

    client = await connect(target_host, namespace)
    await Worker(
        client,
        task_queue=task_queue,
        workflows=[MergedChangeRunnerReadWorkflow],
        activities=[runner_read_step],
    ).run()


async def upsert_schedule(
    client: Any, cfg: dict[str, Any], task_queue: str
) -> dict[str, str]:
    """Create the schedule, or update an existing one to the contract's interval and plan."""
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

    from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.temporal_runner_read import (
        WORKFLOW_NAME,
    )

    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            WORKFLOW_NAME,
            build_plan(cfg).model_dump_json(),
            id=str(cfg["workflow_id_prefix"]),
            task_queue=task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(
                    every=timedelta(seconds=int(cfg["interval_seconds"]))
                )
            ]
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )
    schedule_id = str(cfg["schedule_id"])
    try:
        await client.create_schedule(schedule_id, schedule, trigger_immediately=True)
        return {"schedule_id": schedule_id, "schedule": "created"}
    except ScheduleAlreadyRunningError:

        def _update(_: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(schedule=schedule)

        await client.get_schedule_handle(schedule_id).update(_update)
        return {"schedule_id": schedule_id, "schedule": "updated"}


async def start_once(
    client: Any, cfg: dict[str, Any], task_queue: str, *, publish: bool, wait: bool
) -> dict[str, Any]:
    from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.temporal_runner_read import (
        WORKFLOW_NAME,
    )

    workflow_id = (
        f"{cfg['workflow_id_prefix']}-once-{_now().strftime('%Y%m%dT%H%M%SZ')}"
    )
    handle = await client.start_workflow(
        WORKFLOW_NAME,
        build_plan(cfg, publish=publish).model_dump_json(),
        id=workflow_id,
        task_queue=task_queue,
    )
    out: dict[str, Any] = {"workflow_id": workflow_id, "run_id": handle.result_run_id}
    if wait:
        out["result"] = json.loads(await handle.result())
    return out


def main(argv: list[str] | None = None) -> int:
    cfg = contract_config()
    ap = argparse.ArgumentParser(prog=NODE)
    sub = ap.add_subparsers(dest="verb", required=True)
    for verb in ("worker", "schedule", "start"):
        p = sub.add_parser(verb)
        p.add_argument(
            "--target-host",
            default=os.environ.get("ONEX_TEMPORAL_ADDRESS") or cfg["address"],
        )
        p.add_argument(
            "--namespace",
            default=os.environ.get("ONEX_TEMPORAL_NAMESPACE") or cfg["namespace"],
        )
        p.add_argument("--task-queue", default=cfg["task_queue"])
    sub.choices["worker"].add_argument("--probe", action="store_true")
    sub.choices["worker"].add_argument("--omni-home", default="")
    sub.choices["worker"].add_argument("--state-dir", default="")
    sub.choices["start"].add_argument("--wait", action="store_true")
    sub.choices["start"].add_argument("--no-publish", action="store_true")
    sub.add_parser("lines")
    sub.add_parser("publish")
    args = ap.parse_args(argv)

    if args.verb == "lines":
        return cmd_lines(cfg)
    if args.verb == "publish":
        return cmd_publish(cfg)
    if args.verb == "worker":
        if args.probe:
            from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers import (
                temporal_runner_read,
            )

            sys.stdout.write(
                json.dumps(
                    {"probe": "ok", "workflow": temporal_runner_read.WORKFLOW_NAME}
                )
                + "\n"
            )
            return 0
        asyncio.run(run_worker(args.target_host, args.namespace, args.task_queue))
        return 0

    async def _run() -> dict[str, Any]:
        client = await connect(args.target_host, args.namespace)
        if args.verb == "schedule":
            return await upsert_schedule(client, cfg, args.task_queue)
        return await start_once(
            client, cfg, args.task_queue, publish=not args.no_publish, wait=args.wait
        )

    sys.stdout.write(json.dumps(asyncio.run(_run()), indent=2) + "\n")
    return 0


__all__ = ["build_plan", "build_request", "main", "merge_facts"]
