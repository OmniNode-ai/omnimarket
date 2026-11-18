# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_floor_fill_temporal_workflow <verb>``: the node's declared CLI.

Verbs:

* ``worker``: run the Temporal worker for workflow 2 against a server (``--target-host``, ``--namespace``).
* ``schedule``: ``create`` the Schedule (every 15 minutes, overlap policy SKIP), ``describe`` it, or ``delete`` it.
* ``read-pool``: a step command. Runs the fill node's declared pool read and answers free slots and unowned work.
* ``run-fill``: a step command. Runs the fill node's declared one-run command once and answers the lanes it placed
  and why it placed none.
* ``read-claims``: a step command. Reads the ledger for the CLAIM row of each placed lane inside the claim window.
* ``write-alarm``: a step command. Applies the two alarm rules and appends each alarm row through the ledger CLI
  (``onex-ledger``, which writes through the work ledger append node over the bus on a bus host); a repeat delivery of
  the same step key appends nothing.

The fill node's two commands are declared in the contract (``fill_one_run_argv``, ``pool_read_argv``); while one is
empty the step refuses (exit 3) and names it, so no fill logic is ever substituted here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_floor_fill_temporal_workflow.handlers import (
    HandlerFloorFillAlarmRule,
    HandlerFloorFillClaimRead,
)
from omnimarket.nodes.node_floor_fill_temporal_workflow.models.model_floor_fill_temporal import (
    ModelFloorFillAlarmAnswer,
    ModelFloorFillAlarmRequest,
    ModelFloorFillAlarmRule,
    ModelFloorFillClaimReadRequest,
    ModelFloorFillPoolAnswer,
    ModelFloorFillRunAnswer,
    ModelFloorFillStepInput,
    ModelFloorFillTemporalPlan,
    ModelFloorFillTemporalSteps,
)

NODE = "node_floor_fill_temporal_workflow"
EXIT_UNDECLARED = 3
LEDGER_TAIL_ROWS = 20000
ENV_FILL_ARGV = "ONEX_FLOOR_FILL_ONE_RUN_ARGV"
ENV_POOL_ARGV = "ONEX_FLOOR_FILL_POOL_READ_ARGV"
ENV_LEDGER_APPEND_ARGV = "ONEX_FLOOR_FILL_LEDGER_ARGV"
ENV_LEDGER_READ_ARGV = "ONEX_FLOOR_FILL_LEDGER_READ_ARGV"
ENV_STEP_STATE_DIR = "ONEX_FLOOR_FILL_STEP_STATE_DIR"
ENV_STEP_KEY = "ONEX_RUN_STEP_KEY"


def contract_config() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text)["floor_fill_schedule"])


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _argv(template: list[str]) -> tuple[str, ...]:
    """The contract's argv with ``{python}`` as this interpreter, so the worker runs the steps in its own venv."""
    return tuple(
        sys.executable if part == "{python}" else str(part) for part in template
    )


def build_plan(cfg: dict[str, Any]) -> ModelFloorFillTemporalPlan:
    steps = {name: _argv(list(argv)) for name, argv in cfg["steps"].items()}
    return ModelFloorFillTemporalPlan(
        steps=ModelFloorFillTemporalSteps(**steps),
        rule=ModelFloorFillAlarmRule(**cfg["rule"]),
        claim_poll_seconds=int(cfg["claim_poll_seconds"]),
        step_timeout_seconds=int(cfg["step_timeout_seconds"]),
        step_max_attempts=int(cfg["step_max_attempts"]),
    )


def _declared(env_name: str, cfg_key: str, cfg: dict[str, Any]) -> tuple[str, ...]:
    raw = os.environ.get(env_name, "").strip()
    if raw:
        return tuple(str(p) for p in json.loads(raw))
    return _argv(list(cfg.get(cfg_key) or []))


def _read_step_input() -> ModelFloorFillStepInput:
    return ModelFloorFillStepInput.model_validate_json(sys.stdin.read())


def _last_json(text: str) -> dict[str, Any] | None:
    lines = text.strip().splitlines()
    if not lines:
        return None
    try:
        loaded = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return dict(loaded) if isinstance(loaded, dict) else None


def _run_declared(
    argv: tuple[str, ...], stdin: str, timeout: int
) -> dict[str, Any] | None:
    """Run a declared node command and read its last output line as its answer.

    The fill node answers a refusal (an unset store, an unread pool) as a typed ``status`` line beside a failing exit
    code: that line is its answer and is read as one. A failing exit with no such line is a failed attempt.
    """
    proc = subprocess.run(
        argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False
    )
    answer = _last_json(proc.stdout)
    if proc.returncode != 0 and not (answer is not None and "status" in answer):
        sys.stderr.write(
            f"{argv[0]} exited {proc.returncode}: {proc.stderr.strip()[-300:]}\n"
        )
        return None
    return answer if answer is not None else {}


def _refuse_undeclared(what: str, cfg_key: str) -> int:
    sys.stderr.write(
        f"refused: the fill node's {what} is not declared ({cfg_key} in {NODE}/contract.yaml is empty)\n"
    )
    return EXIT_UNDECLARED


def cmd_read_pool(cfg: dict[str, Any]) -> int:
    step = _read_step_input()
    argv = _declared(ENV_POOL_ARGV, "pool_read_argv", cfg)
    if not argv:
        return _refuse_undeclared("pool read", "pool_read_argv")
    raw = _run_declared(argv, step.model_dump_json(), int(cfg["step_timeout_seconds"]))
    if raw is None:
        return 1
    free = raw.get("free_slots", raw.get("free_slots_total"))
    answer = ModelFloorFillPoolAnswer(
        free_slots=int(free) if free is not None else 0,
        unowned_work=int(raw.get("unowned_work") or 0),
        detail=str(raw.get("detail", "")),
        read_at=str(raw.get("read_at") or _now()),
        unread=str(raw.get("unread") or ""),
    )
    sys.stdout.write(answer.model_dump_json() + "\n")
    return 0


def _placed_lanes(raw: dict[str, Any]) -> tuple[str, ...]:
    """The lanes the fill placed: ``placed_lanes``, else each ``started`` key result's lane (the fill's typed answer)."""
    if "placed_lanes" in raw:
        return tuple(str(lane) for lane in raw["placed_lanes"])
    return tuple(
        str(r["lane"])
        for r in raw.get("key_results", [])
        if r.get("kind") == "started" and r.get("lane")
    )


def _memo_path(step_key: str, cfg: dict[str, Any]) -> Path:
    """Where the answer of a finished step key is kept: the worker's ``ONEX_FLOOR_FILL_STEP_STATE_DIR``, else the
    contract's ``step_state_directory`` under the home."""
    directory = os.environ.get(ENV_STEP_STATE_DIR, "").strip()
    base = (
        Path(directory) if directory else Path.home() / str(cfg["step_state_directory"])
    )
    return base / (re.sub(r"[^A-Za-z0-9._-]", "_", step_key) + ".json")


def cmd_run_fill(cfg: dict[str, Any]) -> int:
    step = _read_step_input()
    argv = _declared(ENV_FILL_ARGV, "fill_one_run_argv", cfg)
    if not argv:
        return _refuse_undeclared("one-run command", "fill_one_run_argv")
    # Temporal delivers an activity at least once: a step key whose fill already finished answers what it recorded
    # and places nothing a second time.
    memo = _memo_path(step.step_key, cfg)
    if memo.is_file():
        sys.stdout.write(memo.read_text(encoding="utf-8").strip() + "\n")
        return 0
    raw = _run_declared(argv, step.model_dump_json(), int(cfg["step_timeout_seconds"]))
    if raw is None:
        return 1
    placed = _placed_lanes(raw)
    lines = [str(line) for line in raw.get("lines", [])]
    why = str(raw.get("why_none") or (lines[0] if lines else raw.get("status", "")))
    free = raw.get("free_slots")
    answer = ModelFloorFillRunAnswer(
        placed_lanes=placed,
        why_none="" if placed else why or "the fill gave no reason",
        free_slots=int(free) if free is not None else None,
        read_at=str(raw.get("read_at") or _now()),
        unread=str(raw.get("unread") or ""),
    )
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(answer.model_dump_json() + "\n", encoding="utf-8")
    sys.stdout.write(answer.model_dump_json() + "\n")
    return 0


def _ledger_rows() -> tuple[str, ...]:
    raw = os.environ.get(ENV_LEDGER_READ_ARGV, "").strip()
    if raw:
        proc = subprocess.run(
            [str(p) for p in json.loads(raw)],
            capture_output=True,
            text=True,
            check=True,
        )
        text = proc.stdout
    else:
        ledger = os.environ.get("ONEX_LEDGER_PATH", "").strip()
        if not ledger:
            raise SystemExit("refused: ONEX_LEDGER_PATH names no ledger")
        text = Path(ledger).read_text(encoding="utf-8")
    return tuple(text.splitlines()[-LEDGER_TAIL_ROWS:])


def cmd_read_claims(cfg: dict[str, Any]) -> int:
    step = _read_step_input()
    if step.fill is None:
        raise SystemExit("refused: read-claims needs the fill step's answer")
    answer = HandlerFloorFillClaimRead().handle(
        ModelFloorFillClaimReadRequest(
            rows=_ledger_rows(),
            lanes=step.fill.placed_lanes,
            placed_at=step.fill.read_at,
            window_seconds=step.rule.claim_window_seconds,
            read_at=_now(),
        )
    )
    sys.stdout.write(answer.model_dump_json() + "\n")
    return 0


def _ledger_append_argv(row: str) -> list[str]:
    raw = os.environ.get(ENV_LEDGER_APPEND_ARGV, "").strip()
    if raw:
        return [*json.loads(raw), row]
    ledger = os.environ.get("ONEX_LEDGER_PATH", "").strip()
    home = os.environ.get("OMNIBASE_INTERNAL_HOME", "").strip()
    if not ledger or not home:
        raise SystemExit(
            "refused: ONEX_LEDGER_PATH or OMNIBASE_INTERNAL_HOME names no ledger tool"
        )
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


def cmd_write_alarm(cfg: dict[str, Any]) -> int:
    step = _read_step_input()
    if step.pool is None or step.fill is None:
        raise SystemExit("refused: write-alarm needs the pool and fill answers")
    decision = HandlerFloorFillAlarmRule().handle(
        ModelFloorFillAlarmRequest(
            rule=step.rule,
            pool=step.pool,
            fill=step.fill,
            claims=step.claims,
            prior_zero_streak=step.prior_zero_streak,
            workflow_id=step.workflow_id,
            step_key=step.step_key,
            lane=os.environ.get("ONEX_FLOOR_FILL_LANE", cfg["lane"]),
            ticket=cfg["ticket"],
            written_at=_now(),
        )
    )
    written = 0
    # The counts row and each alarm row carry their own step token; a repeat delivery of this step finds the rows it
    # already wrote on the ledger and appends only what is missing.
    existing = _ledger_rows()
    for row in (decision.status_row, *decision.rows):
        token = re.search(r"step=(\S+)", row)
        if token is not None and any(
            f"step={token.group(1)} " in r or r.endswith(f"step={token.group(1)}")
            for r in existing
        ):
            continue
        proc = subprocess.run(
            _ledger_append_argv(row), capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            sys.stderr.write(
                f"ledger append exited {proc.returncode}: {proc.stderr[-300:]}\n"
            )
            return proc.returncode
        written += 1
    answer = ModelFloorFillAlarmAnswer(
        zero_streak=decision.zero_streak, alarms=decision.alarms, written=written
    )
    sys.stdout.write(answer.model_dump_json() + "\n")
    return 0


def build_schedule(cfg: dict[str, Any], task_queue: str) -> Any:
    """The Schedule: every ``every_seconds`` (300), overlap policy SKIP, starting one workflow per fire."""
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
    )

    from omnimarket.nodes.node_floor_fill_temporal_workflow.handlers.temporal_floor_fill import (
        WORKFLOW_NAME,
    )

    if cfg["overlap"] != "skip":
        raise ValueError(
            "workflow 2's Schedule skips a fire while a run is in progress"
        )
    return Schedule(
        action=ScheduleActionStartWorkflow(
            WORKFLOW_NAME,
            build_plan(cfg).model_dump_json(),
            id=str(cfg["schedule_id"]),
            task_queue=task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(every=timedelta(seconds=int(cfg["every_seconds"])))
            ]
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


async def _connect(target_host: str, namespace: str) -> Any:
    from temporalio.client import Client

    return await Client.connect(target_host, namespace=namespace)


async def run_worker(target_host: str, namespace: str, task_queue: str) -> None:
    from temporalio.worker import Worker

    from omnimarket.nodes.node_floor_fill_temporal_workflow.handlers.temporal_floor_fill import (
        FloorFillScheduleWorkflow,
        FloorFillStepActivity,
    )

    client = await _connect(target_host, namespace)
    await Worker(
        client,
        task_queue=task_queue,
        workflows=[FloorFillScheduleWorkflow],
        activities=[FloorFillStepActivity(dict(os.environ)).floor_fill_step],
    ).run()


async def schedule_action(
    client: Any, cfg: dict[str, Any], action: str, task_queue: str
) -> dict[str, Any]:
    schedule_id = str(cfg["schedule_id"])
    if action == "create":
        await client.create_schedule(schedule_id, build_schedule(cfg, task_queue))
    handle = client.get_schedule_handle(schedule_id)
    if action == "delete":
        await handle.delete()
        return {"schedule_id": schedule_id, "deleted": True}
    desc = await handle.describe()
    return {
        "schedule_id": schedule_id,
        "num_actions": desc.info.num_actions,
        "skipped_overlap": desc.info.num_actions_skipped_overlap,
        "recent": [
            {
                "workflow_id": a.action.workflow_id,
                "run_id": a.action.first_execution_run_id,
                "scheduled_at": a.scheduled_at.isoformat(),
            }
            for a in desc.info.recent_actions
        ],
        "running": [a.workflow_id for a in desc.info.running_actions],
    }


def main(argv: list[str] | None = None) -> int:
    cfg = contract_config()
    ap = argparse.ArgumentParser(prog=NODE)
    sub = ap.add_subparsers(dest="verb", required=True)
    for verb in ("worker", "schedule"):
        p = sub.add_parser(verb)
        p.add_argument("--target-host", required=True)
        p.add_argument("--namespace", default="default")
        p.add_argument("--task-queue", default=cfg["task_queue"])
    sub.choices["schedule"].add_argument(
        "action", choices=["create", "describe", "delete"]
    )
    for verb in ("read-pool", "run-fill", "read-claims", "write-alarm"):
        sub.add_parser(verb)
    args = ap.parse_args(argv)

    steps = {
        "read-pool": cmd_read_pool,
        "run-fill": cmd_run_fill,
        "read-claims": cmd_read_claims,
        "write-alarm": cmd_write_alarm,
    }
    if args.verb in steps:
        return steps[args.verb](cfg)
    if args.verb == "worker":
        asyncio.run(run_worker(args.target_host, args.namespace, args.task_queue))
        return 0

    async def _schedule() -> dict[str, Any]:
        client = await _connect(args.target_host, args.namespace)
        return await schedule_action(client, cfg, args.action, args.task_queue)

    sys.stdout.write(json.dumps(asyncio.run(_schedule()), indent=2) + "\n")
    return 0
