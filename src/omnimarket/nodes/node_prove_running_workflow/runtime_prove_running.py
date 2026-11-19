# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_prove_running_workflow <verb>``: the node's declared CLI.

Verbs:

* ``worker``: run the Temporal worker for both workflows against a server (``--target-host``, ``--namespace``).
* ``start``: read one integration pass batch (``--batch``, a ``ModelIntegrationReadingBatch`` JSON file), and start
  one workflow per NOT RUNNING line, id ``<repo>#<n>@<sha>``; a second start of a running change joins it and a start
  of a finished one is refused.
* ``schedule``: create or update the hourly batch schedule, with an optional immediate trigger.
* ``read-not-running`` and ``group-by-host``: batch step commands that select changes from the integration ledger
  and build child workflow plans for their hosts.
* ``read``: a step command. Reads the step input on stdin, reads the change through
  ``node_merged_change_reading_effect`` (its declared CLI) and answers one ``ModelProveRunningAnswer`` line.
* ``install``: a step command. The declared installer of the change's target, from the contract's ``installers``.
* ``write-row``: a step command. Builds the STATUS or alarm row and appends it through the ledger CLI
  (``onex-ledger``, which writes through the work ledger append node over the bus on a bus host).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

import yaml

from omnimarket.models.integration_reading import ModelIntegrationReadingBatch
from omnimarket.models.model_merged_change_reading import ModelMcrResult
from omnimarket.nodes.node_prove_running_workflow.handlers import (
    HandlerProveRunningRows,
    HandlerProveRunningStarter,
)
from omnimarket.nodes.node_prove_running_workflow.handlers.handler_integration_ledger_reader import (
    read_not_running,
)
from omnimarket.nodes.node_prove_running_workflow.handlers.handler_prove_running_hosts import (
    group_by_host,
)
from omnimarket.nodes.node_prove_running_workflow.models.model_prove_running import (
    EnumProveRunningOutcome,
    ModelProveRunningAnswer,
    ModelProveRunningBatchPlan,
    ModelProveRunningBatchStepInput,
    ModelProveRunningBatchSteps,
    ModelProveRunningChange,
    ModelProveRunningHostGroups,
    ModelProveRunningPlan,
    ModelProveRunningRowRequest,
    ModelProveRunningStartBatch,
    ModelProveRunningStartRequest,
    ModelProveRunningStepInput,
    ModelProveRunningSteps,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)

if TYPE_CHECKING:
    from temporalio.client import Client, Schedule

NODE = "node_prove_running_workflow"
BUS_APPEND_COMMAND_ENV = "ONEX_LEDGER_BUS_APPEND_COMMAND"
HOST_NAME_ENV = "ONEX_LEDGER_HOST_NAME"
APPEND_TIMEOUT_SECONDS = 120
EXIT_REFUSED = 65


def contract_config() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text)["prove_running"])


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _argv(template: list[str]) -> tuple[str, ...]:
    """The contract's step argv with ``{python}`` as this interpreter, so the worker runs the steps in its own venv."""
    return tuple(sys.executable if part == "{python}" else part for part in template)


def build_plan(
    change: ModelProveRunningChange, cfg: dict[str, Any], *, batch_id: str = ""
) -> ModelProveRunningPlan:
    steps = {name: _argv(list(argv)) for name, argv in cfg["steps"].items()}
    return ModelProveRunningPlan(
        change=change,
        steps=ModelProveRunningSteps(**steps),
        wait_seconds=int(cfg["wait_seconds"]),
        max_readings=int(cfg["max_readings"]),
        deadline_seconds=int(cfg["deadline_seconds"]),
        step_timeout_seconds=int(cfg["step_timeout_seconds"]),
        step_max_attempts=int(cfg["step_max_attempts"]),
        batch_id=batch_id,
    )


def build_batch_plan(cfg: dict[str, Any]) -> ModelProveRunningBatchPlan:
    batch = cfg["batch"]
    steps = {name: _argv(list(argv)) for name, argv in batch["steps"].items()}
    return ModelProveRunningBatchPlan(
        steps=ModelProveRunningBatchSteps(**steps),
        limit=int(batch["limit"]),
        step_timeout_seconds=int(batch["step_timeout_seconds"]),
        step_max_attempts=int(batch["step_max_attempts"]),
    )


def build_schedule(cfg: dict[str, Any], task_queue: str) -> Schedule:
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
    )

    from omnimarket.nodes.node_prove_running_workflow.handlers.temporal_prove_running_batch import (
        BATCH_WORKFLOW_NAME,
    )

    batch = cfg["batch"]
    return Schedule(
        action=ScheduleActionStartWorkflow(
            BATCH_WORKFLOW_NAME,
            build_batch_plan(cfg).model_dump_json(),
            id=batch["schedule_id"],
            task_queue=task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(
                    every=timedelta(seconds=int(batch["interval_seconds"]))
                )
            ]
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


def _read_step_input() -> ModelProveRunningStepInput:
    return ModelProveRunningStepInput.model_validate_json(sys.stdin.read())


def cmd_read(cfg: dict[str, Any]) -> int:
    step = _read_step_input()
    change = step.change
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_merged_change_reading_effect",
            "--change",
            f"{change.key}={change.merge_sha}",
        ],
        capture_output=True,
        text=True,
        timeout=int(cfg["read_timeout_seconds"]),
        check=False,
    )
    read_at = _now()
    if proc.returncode != 0:
        answer = ModelProveRunningAnswer(
            installed=False,
            running=False,
            host=change.target,
            revision=change.merge_sha[:12],
            observed="unread",
            detail=f"merged change reading exited {proc.returncode}: {proc.stderr.strip()[-200:]}",
            read_at=read_at,
        )
    else:
        result = ModelMcrResult.model_validate(json.loads(proc.stdout)["reading"])
        reading = result.readings[0]
        running = reading.verdict == cfg["verdict_running"]
        answer = ModelProveRunningAnswer(
            installed=reading.in_build is True,
            running=running,
            host=change.target,
            revision=(reading.merge_sha or change.merge_sha)[:12],
            observed=(
                f"{reading.verdict} {reading.kind} last_result={reading.last_result or 'none'}"
                f" last_run_at={reading.last_run_at.isoformat() if reading.last_run_at else 'none'}"
            ),
            detail=f"{reading.missing_step} {reading.detail}".strip(),
            read_at=read_at,
        )
    sys.stdout.write(answer.model_dump_json() + "\n")
    return 0


def cmd_install(cfg: dict[str, Any]) -> int:
    step = _read_step_input()
    installers: dict[str, Any] = cfg["installers"]
    entry = installers.get(step.change.target) or installers["default"]
    detail = str(entry["detail"])
    argv = list(entry.get("argv") or [])
    if argv:
        proc = subprocess.run(_argv(argv), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            sys.stderr.write(
                f"installer exited {proc.returncode}: {proc.stderr[-300:]}\n"
            )
            return proc.returncode
        detail = f"{detail}: {proc.stdout.strip()[-200:]}"
    sys.stdout.write(json.dumps({"installer": entry["name"], "detail": detail}) + "\n")
    return 0


def _append_row(row: str, step_key: str, lane: str) -> tuple[int, str]:
    """Append one row. A test names the append command in ``ONEX_PROVE_RUNNING_LEDGER_ARGV``; otherwise the row goes
    through the bus append command the unit's environment names (the ledger file is never named). The request id is
    derived from the step key, so a step delivered twice is one request the ledger host answers once."""
    raw = os.environ.get("ONEX_PROVE_RUNNING_LEDGER_ARGV", "").strip()
    if raw:
        proc = subprocess.run(
            [*json.loads(raw), row], capture_output=True, text=True, check=False
        )
        return proc.returncode, proc.stderr[-300:]
    command = os.environ.get(BUS_APPEND_COMMAND_ENV, "").strip()
    host = os.environ.get(HOST_NAME_ENV, "").strip()
    if not command or not host:
        return 2, f"refused: {BUS_APPEND_COMMAND_ENV} or {HOST_NAME_ENV} is not set"
    outcome = CommandLedgerAppender(
        shlex.split(command), lane, host, APPEND_TIMEOUT_SECONDS
    ).append(row, str(uuid5(NAMESPACE_URL, f"onex:prove-running:{step_key}")))
    if outcome.status in ("accepted", "duplicate"):
        return 0, ""
    return 1, f"ledger bus append {outcome.status}: {outcome.message}"


def cmd_write_row(cfg: dict[str, Any], outcome: str) -> int:
    step = _read_step_input()
    row = (
        HandlerProveRunningRows()
        .handle(
            ModelProveRunningRowRequest(
                outcome=EnumProveRunningOutcome(outcome),
                change=step.change,
                reading=step.last_reading,
                step_key=step.step_key,
                lane=os.environ.get("ONEX_PROVE_RUNNING_LANE", cfg["lane"]),
                ticket=cfg["ticket"],
                written_at=_now(),
                batch_id=step.batch_id,
            )
        )
        .row
    )
    code, error = _append_row(
        row, step.step_key, os.environ.get("ONEX_PROVE_RUNNING_LANE", cfg["lane"])
    )
    if code != 0:
        sys.stderr.write(f"ledger append exited {code}: {error}\n")
        return code
    sys.stdout.write(json.dumps({"written": True, "row": row}) + "\n")
    return 0


def _is_sha(token: object) -> bool:
    return (
        isinstance(token, str)
        and len(token) == 40
        and all(c in "0123456789abcdef" for c in token)
    )


def parse_merge_sha(stdout: str) -> str | None:
    """The merge sha in ``onex scm pr-merge-commit`` output: the verb's JSON line (``result.merge_commit_sha``), or a
    bare 40-hex token on an older verb that printed one."""
    for line in reversed(stdout.splitlines()):
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        result = doc.get("result") if isinstance(doc, dict) else None
        sha = result.get("merge_commit_sha") if isinstance(result, dict) else None
        if _is_sha(sha):
            return str(sha)
    for token in reversed(stdout.split()):
        if _is_sha(token):
            return token
    return None


class CliMergeShaResolver:
    """The merge sha through ``onex scm pr-merge-commit`` (the PR watcher state and the clones, never GitHub)."""

    def __init__(self, owner: str, override_argv: tuple[str, ...] = ()) -> None:
        self._owner = owner
        self._override_argv = override_argv

    def merge_sha(self, repository: str, number: int) -> str | None:
        change_key = f"{repository}#{number}"
        argv = (
            [*self._override_argv, change_key]
            if self._override_argv
            else ["onex", "scm", "pr-merge-commit", f"{self._owner}/{change_key}"]
        )
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        if self._override_argv:
            lines = proc.stdout.strip().splitlines()
            return lines[-1].strip() if proc.returncode == 0 and lines else None
        return parse_merge_sha(proc.stdout)


def _merge_sha_resolver(cfg: dict[str, Any], owner: str) -> CliMergeShaResolver:
    raw = os.environ.get(cfg["merge_sha_argv_env"], "").strip()
    return CliMergeShaResolver(owner, tuple(json.loads(raw)) if raw else ())


def cmd_read_not_running(cfg: dict[str, Any]) -> int:
    step = ModelProveRunningBatchStepInput.model_validate_json(sys.stdin.read())
    env_var = cfg["batch"]["ledger_path_env"]
    path = os.environ.get(env_var, "").strip()
    if not path:
        sys.stderr.write(f"refused: {env_var} names no integration ledger\n")
        return EXIT_REFUSED
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        sys.stderr.write(
            f"refused: unable to read the integration ledger from {env_var}: {exc}\n"
        )
        return EXIT_REFUSED
    batch = read_not_running(text, as_of=datetime.now(UTC), origin=path)
    resolver = _merge_sha_resolver(cfg, cfg["scm_owner"])
    selected = HandlerProveRunningStarter(resolver).handle(
        ModelProveRunningStartRequest(batch=batch, limit=step.limit)
    )
    sys.stdout.write(selected.model_dump_json() + "\n")
    return 0


def cmd_group_by_host(cfg: dict[str, Any]) -> int:
    step = ModelProveRunningBatchStepInput.model_validate_json(sys.stdin.read())
    selected = ModelProveRunningStartBatch.model_validate_json(step.payload)
    batch = cfg["batch"]
    child_cfg = {**cfg, **batch["child"]}
    default_host = batch["default_host"]
    grouped = group_by_host(
        selected.changes,
        lambda change: build_plan(change, child_cfg, batch_id=step.batch_id),
        tuple(batch["hosts"]),
        default_host,
    )
    # Keep the named hosts in order, then run the fallback group.
    result = ModelProveRunningHostGroups(
        groups=tuple(
            sorted(grouped.groups, key=lambda group: group.host == default_host)
        ),
        skipped=selected.skipped,
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


async def _connect(target_host: str, namespace: str) -> Client:
    from temporalio.client import Client

    return await Client.connect(target_host, namespace=namespace)


async def upsert_schedule(
    client: Client, cfg: dict[str, Any], task_queue: str, *, trigger: bool
) -> str:
    from temporalio.client import (
        ScheduleAlreadyRunningError,
        ScheduleUpdate,
        ScheduleUpdateInput,
    )

    schedule_id: str = cfg["batch"]["schedule_id"]
    schedule = build_schedule(cfg, task_queue)

    def update(current: ScheduleUpdateInput) -> ScheduleUpdate:
        return ScheduleUpdate(
            schedule=replace(schedule, state=current.description.schedule.state)
        )

    try:
        handle = await client.create_schedule(schedule_id, schedule)
    except ScheduleAlreadyRunningError:
        handle = client.get_schedule_handle(schedule_id)
        await handle.update(update)
    if trigger:
        await handle.trigger()
    return schedule_id


async def start_changes(
    client: Client,
    changes: tuple[ModelProveRunningChange, ...],
    cfg: dict[str, Any],
    task_queue: str,
) -> list[dict[str, str]]:
    """One start per change, id ``<repo>#<n>@<sha>``: a running one is joined, a finished one is refused."""
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from omnimarket.nodes.node_prove_running_workflow.handlers.temporal_prove_running import (
        WORKFLOW_NAME,
    )

    out: list[dict[str, str]] = []
    for change in changes:
        plan = build_plan(change, cfg)
        try:
            handle = await client.start_workflow(
                WORKFLOW_NAME,
                plan.model_dump_json(),
                id=change.workflow_id,
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
            out.append(
                {
                    "workflow_id": change.workflow_id,
                    "run_id": handle.result_run_id or "",
                    "start": "accepted-or-joined",
                }
            )
        except WorkflowAlreadyStartedError:
            out.append(
                {
                    "workflow_id": change.workflow_id,
                    "run_id": "",
                    "start": "refused-already-proven",
                }
            )
    return out


async def run_worker(target_host: str, namespace: str, task_queue: str) -> None:
    from temporalio.worker import Worker

    from omnimarket.nodes.node_prove_running_workflow.handlers.temporal_prove_running import (
        ProveRunningWorkflow,
        prove_running_step,
    )
    from omnimarket.nodes.node_prove_running_workflow.handlers.temporal_prove_running_batch import (
        ProveRunningBatchWorkflow,
        prove_running_batch_step,
    )

    client = await _connect(target_host, namespace)
    await Worker(
        client,
        task_queue=task_queue,
        workflows=[ProveRunningWorkflow, ProveRunningBatchWorkflow],
        activities=[prove_running_step, prove_running_batch_step],
    ).run()


def main(argv: list[str] | None = None) -> int:
    cfg = contract_config()
    ap = argparse.ArgumentParser(prog=NODE)
    sub = ap.add_subparsers(dest="verb", required=True)
    for verb in ("worker", "start", "schedule"):
        p = sub.add_parser(verb)
        p.add_argument("--target-host", required=True)
        p.add_argument("--namespace", default=cfg["namespace"])
        p.add_argument("--task-queue", default=cfg["task_queue"])
    sub.choices["start"].add_argument(
        "--batch", required=True, help="ModelIntegrationReadingBatch JSON file"
    )
    sub.choices["start"].add_argument("--limit", type=int, default=3)
    sub.choices["start"].add_argument("--owner", default=cfg["scm_owner"])
    sub.choices["start"].add_argument(
        "--wait", action="store_true", help="wait for each result"
    )
    sub.choices["schedule"].add_argument(
        "--schedule-id", default=cfg["batch"]["schedule_id"]
    )
    sub.choices["schedule"].add_argument(
        "--trigger", action="store_true", help="trigger one immediate batch run"
    )
    sub.add_parser("read-not-running")
    sub.add_parser("group-by-host")
    sub.add_parser("read")
    sub.add_parser("install")
    w = sub.add_parser("write-row")
    w.add_argument(
        "--outcome", choices=[o.value for o in EnumProveRunningOutcome], required=True
    )
    args = ap.parse_args(argv)

    if args.verb == "read":
        return cmd_read(cfg)
    if args.verb == "install":
        return cmd_install(cfg)
    if args.verb == "write-row":
        return cmd_write_row(cfg, args.outcome)
    if args.verb == "read-not-running":
        return cmd_read_not_running(cfg)
    if args.verb == "group-by-host":
        return cmd_group_by_host(cfg)
    if args.verb == "worker":
        asyncio.run(run_worker(args.target_host, args.namespace, args.task_queue))
        return 0
    if args.verb == "schedule":
        cfg["batch"]["schedule_id"] = args.schedule_id

        async def _schedule() -> str:
            client = await _connect(args.target_host, args.namespace)
            return await upsert_schedule(
                client, cfg, args.task_queue, trigger=args.trigger
            )

        sys.stdout.write(json.dumps({"schedule_id": asyncio.run(_schedule())}) + "\n")
        return 0

    batch = ModelIntegrationReadingBatch.model_validate_json(
        Path(args.batch).read_text()
    )
    selected = HandlerProveRunningStarter(_merge_sha_resolver(cfg, args.owner)).handle(
        ModelProveRunningStartRequest(batch=batch, limit=args.limit)
    )

    async def _start() -> list[dict[str, str]]:
        client = await _connect(args.target_host, args.namespace)
        started = await start_changes(client, selected.changes, cfg, args.task_queue)
        if args.wait:
            for entry in started:
                entry["result"] = await client.get_workflow_handle(
                    entry["workflow_id"]
                ).result()
        return started

    report = {"started": asyncio.run(_start()), "skipped": list(selected.skipped)}
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if selected.changes else EXIT_REFUSED
