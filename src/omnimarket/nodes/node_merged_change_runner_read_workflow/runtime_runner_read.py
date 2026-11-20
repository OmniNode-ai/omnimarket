# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_merged_change_runner_read_workflow <verb>``: the node's declared CLI.

Verbs:

* ``worker``: run the Temporal worker of the runner read against the contract's server (``--target-host``,
  ``--namespace`` override it). ``--probe`` checks the imports and returns without a connection.
* ``schedule``: create or update the Temporal ready-edge schedule.
* ``start``: start one full read now (``--wait`` prints its result; ``--no-publish`` reads without publishing).
* ``lines``: a step command. Prints the declared runner keys or selects changes in the requested build window.
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
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter
from temporalio.client import Client

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_merged_change_reading import ModelMcrChange
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
    ModelRunnerReadWindow,
    ModelRunnerSelectedLine,
)
from omnimarket.nodes.node_merged_change_runner_read_workflow.models.model_runner_read_ready_edge import (
    ModelRunnerReadReadySettings,
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


def build_plan(
    cfg: dict[str, Any],
    *,
    publish: bool = True,
    previous_build_at: datetime | None = None,
) -> ModelRunnerReadPlan:
    steps = {name: _argv(list(argv)) for name, argv in cfg["steps"].items()}
    return ModelRunnerReadPlan(
        steps=ModelRunnerReadSteps(**steps),
        line_concurrency=int(cfg["line_concurrency"]),
        step_timeout_seconds=int(cfg["step_timeout_seconds"]),
        step_max_attempts=int(cfg["step_max_attempts"]),
        publish=publish,
        previous_build_at=previous_build_at,
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


def cmd_lines(cfg: dict[str, Any], window: ModelRunnerReadWindow | None = None) -> int:
    lines = declared_lines(cfg, os.environ)
    if window is None:
        sys.stdout.write(json.dumps([line.key for line in lines.runners]) + "\n")
        return 0

    changes = tuple(
        ModelMcrChange(repo=line.repo, number=line.number) for line in lines.runners
    )
    resolution_failure: str | None = None
    if changes:
        try:
            changes = resolve_merge_shas(cfg, changes)
        except RuntimeError as exc:
            resolution_failure = str(exc)
    home = Path(os.environ[str(cfg["home_env"])])
    result = select_window_lines(
        changes,
        window.previous_build_at,
        window.as_of,
        lambda repo, sha: merge_facts(home, repo, sha, int(cfg["git_timeout_seconds"])),
        resolution_failure=resolution_failure,
    )
    sys.stdout.write(
        json.dumps([line.model_dump(mode="json") for line in result]) + "\n"
    )
    return 0


def resolve_merge_shas(
    cfg: dict[str, Any], changes: tuple[ModelMcrChange, ...]
) -> tuple[ModelMcrChange, ...]:
    argv = [*_argv(cfg["steps"]["read_line"]), "--resolve-only"]
    for change in changes:
        argv.extend(("--change", f"{change.repo}#{change.number}"))
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=int(cfg["step_timeout_seconds"]),
            check=True,
        )
        resolved = TypeAdapter(tuple[ModelMcrChange, ...]).validate_json(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError(f"merge resolution failed: {type(exc).__name__}") from exc
    if [(c.repo, c.number) for c in changes] != [(c.repo, c.number) for c in resolved]:
        raise RuntimeError("merge resolution returned different runner rows")
    return resolved


def select_window_lines(
    changes: Sequence[ModelMcrChange],
    since: datetime,
    until: datetime,
    lookup: Callable[[str, str], ModelMergeFacts],
    *,
    resolution_failure: str | None = None,
) -> tuple[ModelRunnerSelectedLine, ...]:
    selected = []
    for change in changes:
        facts = (
            lookup(change.repo, change.merge_sha)
            if change.merge_sha
            else ModelMergeFacts()
        )
        if facts.merged_at is not None and not since < facts.merged_at <= until:
            continue
        unread_cause = ""
        if not change.merge_sha:
            unread_cause = resolution_failure or "merge SHA could not be resolved"
        elif facts.merged_at is None:
            unread_cause = "merge time could not be read from the canonical clone"
        selected.append(
            ModelRunnerSelectedLine(
                repo=change.repo,
                number=change.number,
                merge_sha=change.merge_sha,
                merge=facts,
                unread_cause=unread_cause,
            )
        )
    return tuple(selected)


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
    facts = payload.merge_facts or {
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
    if payload.publish and payload.windowed and not result.batch.readings:
        published = {
            "published": False,
            "detail": "no declared runner merges in the read window",
        }
    elif payload.publish:
        argv = [sys.executable, "-m", str(cfg["publish_module"]), "--home", str(home)]
        if payload.windowed:
            argv.append("--incremental")
        proc = subprocess.run(
            argv,
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


async def connect(target_host: str, namespace: str) -> Client:
    return await Client.connect(target_host, namespace=namespace)


async def run_worker(target_host: str, namespace: str, task_queue: str) -> None:
    from temporalio.worker import Worker

    from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.ready_edge_runtime import (
        ReadyEdgeActivities,
    )
    from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.temporal_ready_edge import (
        MergedChangeReadReadyEdge,
    )
    from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.temporal_runner_read import (
        MergedChangeReadingWorkflow,
        runner_read_step,
    )

    client = await connect(target_host, namespace)
    cfg = contract_config()
    activities = ReadyEdgeActivities(
        client,
        build_plan(cfg),
        ModelRunnerReadReadySettings.model_validate(cfg["ready_edge"]),
        task_queue=task_queue,
        workflow_id_prefix=str(cfg["workflow_id_prefix"]),
        home=Path(os.environ[str(cfg["home_env"])]),
    )
    await Worker(
        client,
        task_queue=task_queue,
        workflows=[MergedChangeReadingWorkflow, MergedChangeReadReadyEdge],
        activities=[runner_read_step, activities.tick],
    ).run()


async def upsert_schedule(
    client: Client, cfg: dict[str, Any], task_queue: str
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

    from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.temporal_ready_edge import (
        READY_WORKFLOW_NAME,
    )

    settings = ModelRunnerReadReadySettings.model_validate(cfg["ready_edge"])
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            READY_WORKFLOW_NAME,
            settings.tick_timeout_seconds,
            id=f"{cfg['workflow_id_prefix']}-watch",
            task_queue=task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(
                    every=timedelta(seconds=settings.probe_interval_seconds)
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
    client: Client, cfg: dict[str, Any], task_queue: str, *, publish: bool, wait: bool
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
    sub.add_parser("lines").add_argument("--window", action="store_true")
    sub.add_parser("publish")
    args = ap.parse_args(argv)

    if args.verb == "lines":
        window = (
            ModelRunnerReadWindow.model_validate_json(sys.stdin.read())
            if args.window
            else None
        )
        return cmd_lines(cfg, window)
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
