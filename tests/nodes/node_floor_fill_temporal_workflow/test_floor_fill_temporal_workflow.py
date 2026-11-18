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
import io
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
    ModelFloorFillClaimAnswer,
    ModelFloorFillLaneClaim,
    ModelFloorFillPoolAnswer,
    ModelFloorFillRunAnswer,
    ModelFloorFillRunResult,
)
from omnimarket.nodes.node_floor_fill_temporal_workflow.runtime_floor_fill_temporal import (
    build_plan,
    build_schedule,
    cmd_run_fill,
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
    monkeypatch.setenv("ONEX_FLOOR_FILL_STEP_STATE_DIR", str(tmp_path / "step-state"))
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


def _steps(log: Path, *, done: bool = False) -> Counter[str]:
    """``<verb>:<step>`` per recorded start (``done=True``: per finish); the step is the key after the workflow id."""
    if not log.exists():
        return Counter()
    out: Counter[str] = Counter()
    for line in log.read_text().splitlines():
        verb, key = line.split(" ", 1)
        if verb.startswith("done-") != done:
            continue
        out[f"{verb}:{key.split('/', 1)[1] if '/' in key else key}"] += 1
    return out


def _counts_rows(ledger: Path) -> list[str]:
    if not ledger.exists():
        return []
    return [r for r in ledger.read_text().splitlines() if "src=floor-fill-run" in r]


def _alarm_rows(ledger: Path) -> list[str]:
    if not ledger.exists():
        return []
    return [r for r in ledger.read_text().splitlines() if "src=alarm" in r]


async def _result(handle: WorkflowHandle[Any, Any]) -> ModelFloorFillRunResult:
    return ModelFloorFillRunResult.model_validate_json(str(await handle.result()))


# ---- AC1: the Schedule ------------------------------------------------------------------------------------------


def test_schedule_overlap_skip_spec_every_fifteen_minutes() -> None:
    cfg = contract_config()
    schedule = build_schedule(cfg, cfg["task_queue"])
    assert [i.every for i in schedule.spec.intervals] == [timedelta(minutes=15)]
    assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
    action = schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert action.workflow == WORKFLOW_NAME
    assert cfg["every_seconds"] == 900
    assert cfg["overlap"] == "skip"


@pytest.mark.parametrize("seconds", [300, 900])
def test_schedule_overlap_skip_interval_is_the_one_contract_field(seconds: int) -> None:
    cfg = {**contract_config(), "every_seconds": seconds}
    schedule = build_schedule(cfg, cfg["task_queue"])
    assert [i.every for i in schedule.spec.intervals] == [timedelta(seconds=seconds)]
    assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP


def test_schedule_overlap_skip_refuses_a_queueing_policy() -> None:
    cfg = {**contract_config(), "overlap": "buffer_one"}
    with pytest.raises(ValueError, match="skips a fire"):
        build_schedule(cfg, cfg["task_queue"])


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


async def test_status_counts_row_lands_on_the_ledger_once_per_run(
    env: WorkflowEnvironment, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FREE_SLOTS", "8")
    monkeypatch.setenv("FAKE_UNOWNED", "3")
    monkeypatch.setenv("FAKE_PLACED", "lane-a")
    monkeypatch.setenv("FAKE_CLAIMS", "lane-a")
    async with _worker(env.client, "q"):
        result = await _result(await _start(env.client, _cfg(), "q"))
    rows = _counts_rows(files["ledger"])
    assert len(rows) == 1
    for cell in (
        "| STATUS |",
        "free_slots=8",
        "unowned_work=3",
        "placed=1",
        "placed_lanes=lane-a",
        "claimed=1",
        "unclaimed=0",
        "alarm=none",
        f"workflow={result.workflow_id}",
        f"step={result.workflow_id}/write_alarm/counts",
    ):
        assert cell in rows[0], cell
    assert _alarm_rows(files["ledger"]) == []


async def test_status_counts_row_for_a_fill_that_could_not_read_says_so(
    env: WorkflowEnvironment, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    unread = "fill-state:store-dsn-unset:SOME_STORE_DSN"
    monkeypatch.setenv("FAKE_UNREAD", unread)
    async with _worker(env.client, "q"):
        result = await _result(await _start(env.client, _cfg(), "q"))
    # The refusal is an answer: the run ends, nothing is retried into a failed workflow, and nothing reads as idle.
    assert result.pool.unread == unread
    assert result.fill.unread == unread
    assert result.fill.placed_lanes == ()
    assert result.zero_streak == 0
    assert result.alarms == ()
    rows = _counts_rows(files["ledger"])
    assert len(rows) == 1
    assert f"unread={unread}" in rows[0]
    assert "placed=0" in rows[0]
    assert _alarm_rows(files["ledger"]) == []


def _request(
    free: int,
    placed: tuple[str, ...],
    prior: int,
    *,
    claimed: tuple[str, ...] = (),
    unclaimed: tuple[str, ...] = (),
    unread: str = "",
) -> ModelFloorFillAlarmRequest:
    return ModelFloorFillAlarmRequest(
        rule=ModelFloorFillAlarmRule(),
        pool=ModelFloorFillPoolAnswer(
            free_slots=free,
            unowned_work=3,
            read_at="2026-11-16T18:00:00Z",
            unread=unread,
        ),
        fill=ModelFloorFillRunAnswer(
            placed_lanes=placed,
            why_none="" if placed else "held",
            read_at="2026-11-16T18:00:01Z",
            unread=unread,
        ),
        claims=ModelFloorFillClaimAnswer(
            claimed=tuple(
                ModelFloorFillLaneClaim(
                    lane=lane,
                    claimed_at="2026-11-16T18:00:30Z",
                    row=f"2026-11-16T18:00:30Z | CLAIM | lane={lane}",
                )
                for lane in claimed
            ),
            unclaimed=unclaimed,
            read_at="2026-11-16T18:10:01Z",
        )
        if placed
        else None,
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


def test_status_counts_row_names_what_the_run_read_and_did() -> None:
    decision = HandlerFloorFillAlarmRule().handle(
        _request(8, ("lane-a", "lane-b"), 1, claimed=("lane-a",), unclaimed=("lane-b",))
    )
    row = decision.status_row
    for cell in (
        " | STATUS | ",
        "src=floor-fill-run",
        "free_slots=8",
        "unowned_work=3",
        "placed=2",
        "placed_lanes=lane-a,lane-b",
        "claimed=1",
        "unclaimed=1",
        "alarm=floor-fill-no-claim",
        "zero_streak=0",
        "unread=none",
        "step=wf/write_alarm/counts",
    ):
        assert cell in row, cell
    assert "\n" not in row
    assert len(decision.rows) == 1  # the counts row is not an alarm row


def test_status_counts_row_without_an_alarm_says_none() -> None:
    decision = HandlerFloorFillAlarmRule().handle(_request(2, (), 0))
    assert decision.rows == ()
    assert "alarm=none" in decision.status_row
    assert "placed=0" in decision.status_row
    assert "placed_lanes=none" in decision.status_row


def test_no_alarm_when_a_source_was_not_read_a_failed_read_is_not_zero_slots() -> None:
    rule = HandlerFloorFillAlarmRule()
    unread = rule.handle(_request(0, (), 1, unread="pool:unbound:pool"))
    # Not a zero-placement run: the streak is neither raised nor reset, and no alarm row says the fill idled.
    assert (unread.zero_streak, unread.alarms, unread.rows) == (1, (), ())
    assert "unread=pool:unbound:pool" in unread.status_row
    after = rule.handle(_request(8, (), unread.zero_streak))
    assert after.alarms == ("floor-fill-no-placement",)


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
    assert (
        steps["ledger-append:write_alarm"] == 2
    )  # the counts row and the one alarm row
    assert max(v for k, v in steps.items() if not k.startswith("ledger-append")) == 1
    history = await handle.fetch_history()
    scheduled = [
        e
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    assert len(scheduled) == 1 + 1 + 10 + 1
    assert len(_alarm_rows(files["ledger"])) == 1
    assert len(_counts_rows(files["ledger"])) == 1


async def test_worker_restart_mid_run_during_an_activity_completes_each_step_once(
    env: WorkflowEnvironment,
    files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The worker goes away while the fill step is running: that step is delivered again once, and no step finishes twice."""
    hang = tmp_path / "hang"
    hang.write_text("x")
    monkeypatch.setenv("FAKE_HANG", str(hang))
    monkeypatch.setenv("FAKE_FREE_SLOTS", "8")
    monkeypatch.setenv("FAKE_PLACED", "lane-a")
    monkeypatch.setenv("FAKE_CLAIMS", "lane-a")
    cfg = _cfg()
    cfg["step_max_attempts"] = 3
    async with _worker(env.client, "q", cached=False):
        handle = await _start(env.client, cfg, "q")
        for _ in range(300):
            if _steps(files["log"])["fill:run_fill"] == 1:
                break
            await asyncio.sleep(0.1)
        assert _steps(files["log"])["fill:run_fill"] == 1
    hang.unlink()  # the worker is gone, taking the step's process with it
    async with _worker(env.client, "q", cached=False):
        result = await _result(handle)
    steps = _steps(files["log"])
    assert steps["fill:run_fill"] == 2  # started, cut short, delivered again
    finished = _steps(files["log"], done=True)
    assert finished["done-fill:run_fill"] == 1
    assert set(finished.values()) == {1}, finished  # no step finished twice
    assert result.fill.placed_lanes == ("lane-a",)
    assert result.alarms == ()
    assert len(_counts_rows(files["ledger"])) == 1


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
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    step = {
        "step": "run_fill",
        "step_key": "wf/run_fill",
        "workflow_id": "wf",
        "rule": ModelFloorFillAlarmRule().model_dump(),
    }
    monkeypatch.delenv("ONEX_FLOOR_FILL_ONE_RUN_ARGV", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(step)))
    code = cmd_run_fill({**contract_config(), "fill_one_run_argv": []})
    captured = capsys.readouterr()
    assert code == 3, captured.err
    assert "fill_one_run_argv" in captured.err
    assert captured.out == ""


# ---- the step commands against the fill node's own entry -----------------------------------------------------------


def _run_step(
    verb: str, step: str, env: dict[str, str], state: dict[str, Any] | None = None
) -> subprocess.CompletedProcess[str]:
    payload = {
        "step": step,
        "step_key": f"wf/{step}",
        "workflow_id": "wf",
        "rule": ModelFloorFillAlarmRule().model_dump(),
        **(state or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", NODE, verb],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _fill_overlay_root(tmp_path: Path) -> Path:
    root = tmp_path / "overlays"
    (root / "node_floor_fill_orchestrator").mkdir(parents=True)
    section = {
        "scope": "test-floor-fill",
        "host_id": "host-a",
        "url": "file:///nonexistent/factory-floor.json",
        "plans_root": str(tmp_path / "plans"),
        "runner_command": ["node-entry", "--lane", "{lane}", "--brief", "{brief}"],
        "line_brief_text": "Work {task_id} of {line_id} on {ticket} as {lane} ({key}) under {plans_root}",
        "reserved_timeout_seconds": 600,
        "sent_window_minutes": 120,
        "command_timeout_seconds": 60,
        "tick_interval_seconds": 300,
        "remote_argv": ["ssh", "host-a", "node-entry", "{argv}"],
        "process_environment": {"LANG": "C.UTF-8"},
        "process_working_directory": str(tmp_path),
        "max_age_minutes": 15,
        "max_fill_lanes_per_line": 1,
        "stopped_max_lanes": 2,
        "stopped_report_chars": 2000,
        "stopped_continue_rule": "Continue; do not repeat.",
        "stopped_runner_command": [
            "node-entry",
            "--lane",
            "{lane}",
            "--brief",
            "{brief}",
        ],
        "stopped_reports_root": str(tmp_path),
        "stopped_brief_directory": str(tmp_path / "briefs"),
    }
    live = {
        "store_dsn_env": "ONEX_TEST_UNSET_DSN",
        "store_schema_name": "public",
        "store_table_name": "floor_fill_state",
        "store_connect_timeout_seconds": 5,
        "floor_fetch_timeout_seconds": 10.0,
        "plan_file_pattern": "{line_id}.md",
        "ledger_path": str(tmp_path / "ledger.md"),
        "process_output_dir": str(tmp_path / "logs"),
    }
    import yaml

    (root / "node_floor_fill_orchestrator" / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "floor_fill": section,
                "floor_fill_live": live,
                "lab_hosts": [{"name": "host-a", "available": True}],
            }
        ),
        encoding="utf-8",
    )
    return root


def _clean_env(overlays: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ONEX_FLOOR_FILL_", "ONEX_TEST_UNSET_DSN"))
    }
    env["ONEX_SKILL_OVERLAY_ROOTS"] = str(overlays)
    return env


def test_contract_declares_the_fill_nodes_two_commands() -> None:
    cfg = contract_config()
    entry = (
        "omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator"
    )
    for key, command in (("fill_one_run_argv", "tick"), ("pool_read_argv", "inspect")):
        argv = cfg[key]
        assert argv[:3] == ["{python}", "-m", entry], argv
        assert "--overlay-from-roots" in argv, argv
        assert "--answer-json" in argv, argv
        assert argv[argv.index("--profile") + 1] == "live", argv
        assert argv[-1] == command, argv


def test_activities_call_nodes_read_pool_reads_the_real_fill_entrys_refusal_as_unread(
    tmp_path: Path,
) -> None:
    proc = _run_step("read-pool", "read_pool", _clean_env(_fill_overlay_root(tmp_path)))
    assert proc.returncode == 0, proc.stderr
    answer = ModelFloorFillPoolAnswer.model_validate_json(
        proc.stdout.strip().splitlines()[-1]
    )
    assert answer.unread == "fill-state:store-dsn-unset:ONEX_TEST_UNSET_DSN"
    assert answer.free_slots == 0  # never read as a count; ``unread`` is what says so


def test_activities_call_nodes_run_fill_reads_the_real_fill_entrys_refusal_as_unread(
    tmp_path: Path,
) -> None:
    proc = _run_step("run-fill", "run_fill", _clean_env(_fill_overlay_root(tmp_path)))
    assert proc.returncode == 0, proc.stderr
    answer = ModelFloorFillRunAnswer.model_validate_json(
        proc.stdout.strip().splitlines()[-1]
    )
    assert answer.placed_lanes == ()
    assert answer.unread == "fill-state:store-dsn-unset:ONEX_TEST_UNSET_DSN"
    assert "store-dsn-unset" in answer.why_none


def test_run_fill_repeat_delivery_of_one_step_key_runs_the_fill_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "steps.log"
    ledger = tmp_path / "ledger.md"
    env = {
        **os.environ,
        "FAKE_STEP_LOG": str(log),
        "FAKE_LEDGER": str(ledger),
        "FAKE_PLACED": "lane-a",
        "ONEX_FLOOR_FILL_ONE_RUN_ARGV": json.dumps([sys.executable, FAKE, "fill"]),
        "ONEX_FLOOR_FILL_STEP_STATE_DIR": str(tmp_path / "state"),
        "ONEX_RUN_STEP_KEY": "wf/run_fill",
    }
    first = _run_step("run-fill", "run_fill", env)
    second = _run_step("run-fill", "run_fill", env)
    other = _run_step(
        "run-fill",
        "run_fill",
        {**env, "ONEX_RUN_STEP_KEY": "wf2/run_fill"},
        {"step_key": "wf2/run_fill", "workflow_id": "wf2"},
    )
    assert first.returncode == second.returncode == other.returncode == 0
    assert first.stdout == second.stdout  # the recorded answer, not a second placement
    # Two step keys, three deliveries: the node ran twice (the first key once, the other key once).
    assert _steps(log)["fill:run_fill"] == 2
    assert log.read_text().splitlines().count("fill wf/run_fill") == 1
    assert log.read_text().splitlines().count("fill wf2/run_fill") == 1
