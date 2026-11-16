# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_prove_running_workflow <verb>``: the node's declared CLI.

Verbs:

* ``worker``: run the Temporal worker for workflow 1 against a server (``--target-host``, ``--namespace``).
* ``start``: read one integration pass batch (``--batch``, a ``ModelIntegrationReadingBatch`` JSON file), and start
  one workflow per NOT RUNNING line, id ``<repo>#<n>@<sha>``; a second start of a running change joins it and a start
  of a finished one is refused.
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
import subprocess
import sys
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.integration_reading import ModelIntegrationReadingBatch
from omnimarket.models.model_merged_change_reading import ModelMcrResult
from omnimarket.nodes.node_prove_running_workflow.handlers import (
    HandlerProveRunningRows,
    HandlerProveRunningStarter,
)
from omnimarket.nodes.node_prove_running_workflow.models.model_prove_running import (
    EnumProveRunningOutcome,
    ModelProveRunningAnswer,
    ModelProveRunningChange,
    ModelProveRunningPlan,
    ModelProveRunningRowRequest,
    ModelProveRunningStartRequest,
    ModelProveRunningStepInput,
    ModelProveRunningSteps,
)

NODE = "node_prove_running_workflow"
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
    change: ModelProveRunningChange, cfg: dict[str, Any]
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


def _ledger_argv(row: str) -> list[str]:
    raw = os.environ.get("ONEX_PROVE_RUNNING_LEDGER_ARGV", "").strip()
    if raw:
        return [*json.loads(raw), row]
    ledger = os.environ.get("ONEX_LEDGER_PATH", "").strip()
    if not ledger:
        raise SystemExit("refused: ONEX_LEDGER_PATH names no ledger")
    home = os.environ.get("OMNIBASE_INTERNAL_HOME", "").strip()
    if not home:
        raise SystemExit("refused: OMNIBASE_INTERNAL_HOME names no ledger tool project")
    return [
        "uv",
        "run",
        "--frozen",
        "--project",
        home,
        "onex-ledger",
        ledger,
        "--append",
        row,
    ]


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
            )
        )
        .row
    )
    proc = subprocess.run(
        _ledger_argv(row), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"ledger append exited {proc.returncode}: {proc.stderr[-300:]}\n"
        )
        return proc.returncode
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

    def __init__(self, owner: str) -> None:
        self._owner = owner

    def merge_sha(self, repository: str, number: int) -> str | None:
        proc = subprocess.run(
            ["onex", "scm", "pr-merge-commit", f"{self._owner}/{repository}#{number}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return parse_merge_sha(proc.stdout)


async def _connect(target_host: str, namespace: str) -> Any:
    from temporalio.client import Client

    return await Client.connect(target_host, namespace=namespace)


async def start_changes(
    client: Any,
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

    client = await _connect(target_host, namespace)
    await Worker(
        client,
        task_queue=task_queue,
        workflows=[ProveRunningWorkflow],
        activities=[prove_running_step],
    ).run()


def main(argv: list[str] | None = None) -> int:
    cfg = contract_config()
    ap = argparse.ArgumentParser(prog=NODE)
    sub = ap.add_subparsers(dest="verb", required=True)
    for verb in ("worker", "start"):
        p = sub.add_parser(verb)
        p.add_argument("--target-host", required=True)
        p.add_argument("--namespace", default="default")
        p.add_argument("--task-queue", default=cfg["task_queue"])
    sub.choices["start"].add_argument(
        "--batch", required=True, help="ModelIntegrationReadingBatch JSON file"
    )
    sub.choices["start"].add_argument("--limit", type=int, default=3)
    sub.choices["start"].add_argument("--owner", default="OmniNode-ai")
    sub.choices["start"].add_argument(
        "--wait", action="store_true", help="wait for each result"
    )
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
    if args.verb == "worker":
        asyncio.run(run_worker(args.target_host, args.namespace, args.task_queue))
        return 0

    batch = ModelIntegrationReadingBatch.model_validate_json(
        Path(args.batch).read_text()
    )
    selected = HandlerProveRunningStarter(CliMergeShaResolver(args.owner)).handle(
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
