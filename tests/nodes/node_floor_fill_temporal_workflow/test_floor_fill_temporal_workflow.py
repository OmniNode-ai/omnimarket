# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Workflow 2, the floor fill on a Temporal Schedule, against Temporal's test servers.

The Schedule tests run on the SDK's local dev server (the time-skipping test server implements no Schedule calls);
the run tests run on the time-skipping test server. Every step runs a real process: this node's own step verbs, with
the fill node's commands and the ledger CLI pointed at a recorded stand-in (``fake_fill_node.py``) that records each
step key. Counting the recorded keys is how each test sees that a step ran once.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import (
    Client,
    ScheduleActionExecutionStartWorkflow,
    ScheduleActionStartWorkflow,
    ScheduleHandle,
    ScheduleOverlapPolicy,
    WorkflowHandle,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from omnimarket.nodes.node_floor_fill_temporal_workflow.handlers import (
    HandlerFloorFillAlarmRule,
)
from omnimarket.nodes.node_floor_fill_temporal_workflow.handlers import (
    temporal_floor_fill as wf_module,
)
from omnimarket.nodes.node_floor_fill_temporal_workflow.handlers.temporal_floor_fill import (
    ACTIVITY_NAME,
    WORKFLOW_NAME,
    FloorFillScheduleWorkflow,
    FloorFillStepActivity,
)
from omnimarket.nodes.node_floor_fill_temporal_workflow.models import (
    ModelFloorFillAlarmRequest,
    ModelFloorFillAlarmRule,
    ModelFloorFillPoolAnswer,
    ModelFloorFillRunAnswer,
    ModelFloorFillRunResult,
)
from omnimarket.nodes.node_floor_fill_temporal_workflow.runtime_floor_fill_temporal import (
    build_plan,
    build_schedule,
    contract_config,
    schedule_action,
)

pytestmark = pytest.mark.unit

FAKE = str(Path(__file__).with_name("fake_fill_node.py"))
NODE = "omnimarket.nodes.node_floor_fill_temporal_workflow"


def _cfg(**rule: int) -> dict[str, Any]:
    cfg = contract_config()
    cfg["rule"] = {**cfg["rule"], **rule}
    if "claim_window_seconds" in rule:
        cfg["claim_poll_seconds"] = min(
            cfg["claim_poll_seconds"], rule["claim_window_seconds"]
        )
    cfg["step_max_attempts"] = 1
    cfg["schedule_id"] = f"floor-fill-{uuid.uuid4().hex[:8]}"
    return cfg


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    log = tmp_path / "steps.log"
    ledger = tmp_path / "ledger.md"
    py = sys.executable
    monkeypatch.setenv("FAKE_STEP_LOG", str(log))
    monkeypatch.setenv("FAKE_LEDGER", str(ledger))
    monkeypatch.setenv("ONEX_FLOOR_FILL_POOL_READ_ARGV", json.dumps([py, FAKE, "pool"]))
    monkeypatch.setenv("ONEX_FLOOR_FILL_ONE_RUN_ARGV", json.dumps([py, FAKE, "fill"]))
    monkeypatch.setenv(
        "ONEX_FLOOR_FILL_LEDGER_ARGV", json.dumps([py, FAKE, "ledger-append"])
    )
    monkeypatch.setenv(
        "ONEX_FLOOR_FILL_LEDGER_READ_ARGV", json.dumps([py, FAKE, "ledger-read"])
    )
    return {"log": log, "ledger": ledger}


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


@pytest.fixture
async def local_env() -> AsyncIterator[WorkflowEnvironment]:
    environment = await WorkflowEnvironment.start_local()
    try:
        yield environment
    finally:
        await environment.shutdown()


def _worker(client: Client, queue: str, *, cached: bool = True) -> Worker:
    # A worker with no workflow cache holds no sticky queue, so a second worker picks the run up at once: the same
    # as a worker process that was killed and started again (its cache is gone with it).
    return Worker(
        client,
        task_queue=queue,
        workflows=[FloorFillScheduleWorkflow],
        activities=[FloorFillStepActivity(dict(os.environ)).floor_fill_step],
        **({} if cached else {"max_cached_workflows": 0}),
    )


async def _start(
    client: Client, cfg: dict[str, Any], queue: str
) -> WorkflowHandle[Any, Any]:
    return await client.start_workflow(
        WORKFLOW_NAME,
        build_plan(cfg).model_dump_json(),
        id=f"floor-fill-{uuid.uuid4().hex[:8]}",
        task_queue=queue,
    )


def _steps(log: Path) -> Counter[str]:
    """``<verb>:<step>`` per recorded run; the step is the key after the workflow id."""
    if not log.exists():
        return Counter()
    out: Counter[str] = Counter()
    for line in log.read_text().splitlines():
        verb, key = line.split(" ", 1)
        out[f"{verb}:{key.split('/', 1)[1] if '/' in key else key}"] += 1
    return out


def _alarm_rows(ledger: Path) -> list[str]:
    if not ledger.exists():
        return []
    return [r for r in ledger.read_text().splitlines() if "src=alarm" in r]


async def _result(handle: WorkflowHandle[Any, Any]) -> ModelFloorFillRunResult:
    return ModelFloorFillRunResult.model_validate_json(str(await handle.result()))


# ---- AC1: the Schedule ------------------------------------------------------------------------------------------


def test_schedule_overlap_skip_spec_every_five_minutes() -> None:
    cfg = contract_config()
    schedule = build_schedule(cfg, cfg["task_queue"])
    assert [i.every for i in schedule.spec.intervals] == [timedelta(minutes=5)]
    assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
    action = schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert action.workflow == WORKFLOW_NAME
    assert cfg["every_seconds"] == 300
    assert cfg["overlap"] == "skip"


async def _wait_running(handle: ScheduleHandle, count: int) -> None:
    for _ in range(200):
        if len((await handle.describe()).info.running_actions) >= count:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("the scheduled run never started")


async def test_schedule_overlap_skip_run_in_progress_is_skipped(
    local_env: WorkflowEnvironment,
    files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fill places a lane that never claims, so the run waits out its (short) claim window: still in progress.
    monkeypatch.setenv("FAKE_FREE_SLOTS", "8")
    monkeypatch.setenv("FAKE_PLACED", "lane-a")
    cfg = _cfg(claim_window_seconds=6)
    queue = f"q-{uuid.uuid4().hex[:8]}"
    async with _worker(local_env.client, queue):
        await schedule_action(local_env.client, cfg, "create", queue)
        handle = local_env.client.get_schedule_handle(cfg["schedule_id"])
        await handle.trigger()
        await _wait_running(handle, 1)
        await handle.trigger()  # the next fire while the first run is in progress
        await asyncio.sleep(1)
        info = (await handle.describe()).info
        assert len(info.running_actions) == 1
        assert info.num_actions_skipped_overlap >= 1
        first = local_env.client.get_workflow_handle(
            info.running_actions[0].workflow_id
        )
        result = await _result(first)
        info = (await handle.describe()).info
        assert (
            info.num_actions == 1
        )  # skipped, not queued: no second run started after the first ended
        await handle.delete()
    assert result.alarms == ("floor-fill-no-claim",)
    assert _steps(files["log"])["fill:run_fill"] == 1


async def _scheduled_run(
    handle: ScheduleHandle, client: Client, n: int
) -> ModelFloorFillRunResult:
    await handle.trigger()
    for _ in range(200):
        info = (await handle.describe()).info
        if info.num_actions >= n and not info.running_actions:
            break
        await asyncio.sleep(0.1)
    recent = (await handle.describe()).info.recent_actions[-1].action
    assert isinstance(recent, ScheduleActionExecutionStartWorkflow)
    return await _result(client.get_workflow_handle(recent.workflow_id))


# ---- AC4: the alarms --------------------------------------------------------------------------------------------


async def test_alarm_no_placement_two_scheduled_runs_in_a_row(
    local_env: WorkflowEnvironment,
    files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_FREE_SLOTS", "8")
    monkeypatch.setenv("FAKE_PLACED", "")
    monkeypatch.setenv("FAKE_WHY", "every unowned item is held")
    cfg = _cfg()
    queue = f"q-{uuid.uuid4().hex[:8]}"
    async with _worker(local_env.client, queue):
        await schedule_action(local_env.client, cfg, "create", queue)
        handle = local_env.client.get_schedule_handle(cfg["schedule_id"])
        first = await _scheduled_run(handle, local_env.client, 1)
        assert first.zero_streak == 1
        assert first.alarms == ()
        assert _alarm_rows(files["ledger"]) == []
        second = await _scheduled_run(handle, local_env.client, 2)
        await handle.delete()
    assert second.zero_streak == 2
    assert second.alarms == ("floor-fill-no-placement",)
    rows = _alarm_rows(files["ledger"])
    assert len(rows) == 1
    assert "alarm=floor-fill-no-placement" in rows[0]
    assert "free_slots=8" in rows[0]
    assert "placed=0" in rows[0]
    assert "zero_streak=2" in rows[0]
    assert "why_none=every unowned item is held" in rows[0]
    assert second.fill.why_none == "every unowned item is held"


async def test_no_alarm_when_free_slots_at_or_below_five(
    local_env: WorkflowEnvironment,
    files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_FREE_SLOTS", "5")
    monkeypatch.setenv("FAKE_PLACED", "")
    cfg = _cfg()
    queue = f"q-{uuid.uuid4().hex[:8]}"
    async with _worker(local_env.client, queue):
        await schedule_action(local_env.client, cfg, "create", queue)
        handle = local_env.client.get_schedule_handle(cfg["schedule_id"])
        results = [await _scheduled_run(handle, local_env.client, n) for n in (1, 2)]
        await handle.delete()
    assert [r.zero_streak for r in results] == [0, 0]
    assert all(r.alarms == () for r in results)
    assert _alarm_rows(files["ledger"]) == []


async def test_alarm_no_claim_placed_lane_without_claim_in_ten_minutes(
    env: WorkflowEnvironment, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FREE_SLOTS", "8")
    monkeypatch.setenv("FAKE_PLACED", "lane-a,lane-b")
    monkeypatch.setenv("FAKE_CLAIMS", "lane-b")
    async with _worker(env.client, "q"):
        result = await _result(await _start(env.client, _cfg(), "q"))
    assert result.alarms == ("floor-fill-no-claim",)
    assert result.claims is not None
    assert result.claims.unclaimed == ("lane-a",)
    assert [c.lane for c in result.claims.claimed] == ["lane-b"]
    assert (
        result.claim_readings == 10
    )  # read every minute until the 10-minute window closed
    rows = _alarm_rows(files["ledger"])
    assert len(rows) == 1
    assert "alarm=floor-fill-no-claim" in rows[0]
    assert "unclaimed=lane-a" in rows[0]
    assert "within 10 minutes" in rows[0]


async def test_no_alarm_when_every_placed_lane_claims(
    env: WorkflowEnvironment, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FREE_SLOTS", "8")
    monkeypatch.setenv("FAKE_PLACED", "lane-a")
    monkeypatch.setenv("FAKE_CLAIMS", "lane-a")
    async with _worker(env.client, "q"):
        result = await _result(await _start(env.client, _cfg(), "q"))
    assert result.alarms == ()
    assert result.zero_streak == 0
    assert result.claim_readings == 1
    assert result.claims is not None
    assert result.claims.unclaimed == ()
    assert _alarm_rows(files["ledger"]) == []


def _request(
    free: int, placed: tuple[str, ...], prior: int
) -> ModelFloorFillAlarmRequest:
    return ModelFloorFillAlarmRequest(
        rule=ModelFloorFillAlarmRule(),
        pool=ModelFloorFillPoolAnswer(
            free_slots=free, unowned_work=3, read_at="2026-11-16T18:00:00Z"
        ),
        fill=ModelFloorFillRunAnswer(
            placed_lanes=placed,
            why_none="" if placed else "held",
            read_at="2026-11-16T18:00:01Z",
        ),
        prior_zero_streak=prior,
        workflow_id="wf",
        step_key="wf/write_alarm",
        lane="floor-fill-schedule",
        ticket="OMN-24927",
        written_at="2026-11-16T18:00:02Z",
    )


def test_no_alarm_rule_cases() -> None:
    rule = HandlerFloorFillAlarmRule()
    first = rule.handle(_request(8, (), 0))
    assert (first.zero_streak, first.alarms, first.rows) == (1, (), ())
    placed = rule.handle(_request(8, ("lane-a",), 1))
    assert (placed.zero_streak, placed.alarms) == (0, ())
    low = rule.handle(_request(5, (), 1))
    assert (low.zero_streak, low.alarms) == (0, ())
    second = rule.handle(_request(6, (), 1))
    assert second.alarms == ("floor-fill-no-placement",)
    assert len(second.rows) == 1


# ---- AC3: a worker restart mid-run ------------------------------------------------------------------------------


async def test_worker_restart_mid_run_runs_no_activity_twice(
    env: WorkflowEnvironment, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FREE_SLOTS", "8")
    monkeypatch.setenv("FAKE_PLACED", "lane-a")
    async with _worker(env.client, "q", cached=False):
        handle = await _start(env.client, _cfg(), "q")
        for _ in range(200):
            if _steps(files["log"])["fill:run_fill"] == 1:
                break
            await asyncio.sleep(0.1)
        # Let the workflow task that schedules the first durable claim wait complete before the worker goes away.
        for _ in range(100):
            history = await handle.fetch_history()
            if any(
                e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
                for e in history.events
            ):
                break
            await asyncio.sleep(0.1)
    assert _steps(files["log"]) == Counter({"pool:read_pool": 1, "fill:run_fill": 1})
    # A new worker (the old one is gone): the timer fires on the server and the run resumes from its history.
    async with _worker(env.client, "q", cached=False):
        result = await _result(handle)
    assert result.alarms == ("floor-fill-no-claim",)
    steps = _steps(files["log"])
    assert steps["pool:read_pool"] == 1
    assert steps["fill:run_fill"] == 1
    assert [steps[f"ledger-read:read_claims/{n}"] for n in range(1, 11)] == [1] * 10
    assert steps["ledger-read:write_alarm"] == 1  # the repeat-delivery check
    assert steps["ledger-append:write_alarm"] == 1  # the one alarm row
    assert max(steps.values()) == 1
    history = await handle.fetch_history()
    scheduled = [
        e
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    assert len(scheduled) == 1 + 1 + 10 + 1
    assert len(_alarm_rows(files["ledger"])) == 1


# ---- AC2: every step is an activity that calls one of our nodes -------------------------------------------------


async def test_activities_call_nodes(
    env: WorkflowEnvironment, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FREE_SLOTS", "8")
    monkeypatch.setenv("FAKE_PLACED", "lane-a")
    monkeypatch.setenv("FAKE_CLAIMS", "lane-a")
    async with _worker(env.client, "q"):
        handle = await _start(env.client, _cfg(), "q")
        await handle.result()
        history = await handle.fetch_history()
    scheduled = [
        e.activity_task_scheduled_event_attributes
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    assert {a.activity_type.name for a in scheduled} == {ACTIVITY_NAME}
    # Every step the workflow ordered ran as a node process that recorded its key; an inlined step records nothing.
    steps = _steps(files["log"])
    assert steps["pool:read_pool"] == 1
    assert steps["fill:run_fill"] == 1
    assert steps["ledger-read:read_claims/1"] == 1
    assert len(scheduled) == 4
    plan = build_plan(contract_config())
    for argv, attrs in zip(
        (
            plan.steps.read_pool,
            plan.steps.run_fill,
            plan.steps.read_claims,
            plan.steps.write_alarm,
        ),
        scheduled,
        strict=True,
    ):
        assert attrs.input.payloads, "an activity carries its node command"
        assert json.loads(attrs.input.payloads[0].data)[1:] == list(argv[1:])
    # The workflow body runs no process, touches no file or network, and holds no fill or alarm logic itself.
    body = inspect.getsource(FloorFillScheduleWorkflow)
    for forbidden in (
        "subprocess",
        "open(",
        "os.",
        "socket",
        "httpx",
        "run_step_process",
        "free_slots",
        "free_slots_above",
        "zero_runs",
        "key_results",
        "HandlerFloorFill",
        "node_floor_fill_orchestrator",
    ):
        assert forbidden not in body, forbidden
    assert body.count("await step(") == 4
    assert "run_step_process" in inspect.getsource(wf_module.FloorFillStepActivity)


def test_activities_call_nodes_contract_steps_name_node_commands() -> None:
    cfg = contract_config()
    assert set(cfg["steps"]) == {"read_pool", "run_fill", "read_claims", "write_alarm"}
    for argv in cfg["steps"].values():
        assert argv[:3] == ["{python}", "-m", NODE], argv


def test_activities_call_nodes_run_fill_refuses_while_the_one_run_command_is_undeclared(
    tmp_path: Path,
) -> None:
    step = {
        "step": "run_fill",
        "step_key": "wf/run_fill",
        "workflow_id": "wf",
        "rule": ModelFloorFillAlarmRule().model_dump(),
    }
    env = {
        k: v
        for k, v in __import__("os").environ.items()
        if not k.startswith("ONEX_FLOOR_FILL_")
    }
    proc = subprocess.run(
        [sys.executable, "-m", NODE, "run-fill"],
        input=json.dumps(step),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 3, proc.stderr
    assert "fill_one_run_argv" in proc.stderr
    assert proc.stdout == ""
