# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Workflow 1, prove-running, against Temporal's time-skipping test server.

Every step runs a real process: the read and install steps a stand-in node (``fake_node.py``) that records each step
key, the write steps this node's own ``write-row`` CLI with the ledger CLI pointed at the stand-in. Counting the
recorded step keys is how each test sees that a step ran once.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowHandle
from temporalio.common import (
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from omnimarket.nodes.node_prove_running_workflow.handlers import (
    temporal_prove_running as wf_module,
)
from omnimarket.nodes.node_prove_running_workflow.handlers.temporal_prove_running import (
    ACTIVITY_NAME,
    WORKFLOW_NAME,
    ProveRunningWorkflow,
    prove_running_step,
)
from omnimarket.nodes.node_prove_running_workflow.models import (
    EnumProveRunningOutcome,
    ModelProveRunningChange,
    ModelProveRunningPlan,
    ModelProveRunningResult,
    ModelProveRunningSteps,
)

pytestmark = pytest.mark.unit

FAKE = str(Path(__file__).with_name("fake_node.py"))
NODE = "omnimarket.nodes.node_prove_running_workflow"
QUEUE = "prove-running-test"
CHANGE = ModelProveRunningChange(
    repository="omnimarket",
    number=6090,
    merge_sha="d2985cccba1234567890",
    target="dev-runtime",
)


def _plan(**overrides: int) -> ModelProveRunningPlan:
    py = sys.executable
    return ModelProveRunningPlan(
        change=CHANGE,
        steps=ModelProveRunningSteps(
            read_status=(py, FAKE, "read"),
            install=(py, FAKE, "install"),
            read_running=(py, FAKE, "read"),
            write_status=(py, "-m", NODE, "write-row", "--outcome", "proved"),
            write_alarm=(py, "-m", NODE, "write-row", "--outcome", "alarm"),
        ),
        wait_seconds=overrides.get("wait_seconds", 300),
        max_readings=overrides.get("max_readings", 4),
        deadline_seconds=overrides.get("deadline_seconds", 3600),
        step_max_attempts=1,
    )


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    log = tmp_path / "steps.log"
    ledger = tmp_path / "ledger.md"
    monkeypatch.setenv("FAKE_STEP_LOG", str(log))
    monkeypatch.setenv("FAKE_LEDGER", str(ledger))
    monkeypatch.setenv(
        "ONEX_PROVE_RUNNING_LEDGER_ARGV",
        json.dumps([sys.executable, FAKE, "ledger-append"]),
    )
    return {"log": log, "ledger": ledger}


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


def _worker(client: Client, *, cached: bool = True) -> Worker:
    # A worker with no workflow cache holds no sticky queue, so a second worker picks the run up at once: the same
    # as a worker process that was killed and started again (its cache is gone with it).
    return Worker(
        client,
        task_queue=QUEUE,
        workflows=[ProveRunningWorkflow],
        activities=[prove_running_step],
        **({} if cached else {"max_cached_workflows": 0}),
    )


async def _start(
    client: Client, plan: ModelProveRunningPlan
) -> WorkflowHandle[object, object]:
    return await client.start_workflow(
        WORKFLOW_NAME,
        plan.model_dump_json(),
        id=plan.change.workflow_id,
        task_queue=QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


def _steps(log: Path) -> Counter[str]:
    if not log.exists():
        return Counter()
    return Counter(
        line.split(" ", 1)[1].split("/", 1)[1] for line in log.read_text().splitlines()
    )


async def test_workflow_id_is_repo_number_at_sha() -> None:
    assert CHANGE.workflow_id == "omnimarket#6090@d2985cccba1234567890"


async def test_proves_running_writes_status_row(
    env: WorkflowEnvironment, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_INSTALLED", "1")
    monkeypatch.setenv("FAKE_RUNNING_AFTER", "2")
    async with _worker(env.client):
        handle = await _start(env.client, _plan())
        result = ModelProveRunningResult.model_validate_json(str(await handle.result()))
    assert result.outcome is EnumProveRunningOutcome.PROVED
    assert result.readings == 2
    assert result.installed_by_workflow is False
    rows = files["ledger"].read_text().splitlines()
    assert len(rows) == 1
    row = rows[0]
    assert " | STATUS | " in row
    assert "pr=omnimarket#6090" in row
    assert "installed=dev-runtime:d2985cccba12" in row
    assert "observed=dispatch seen in the runtime log" in row
    assert "read_at=2026-11-16T16:02:00Z" in row
    steps = _steps(files["log"])
    assert steps["read_status"] == 1
    assert steps["install"] == 0
    assert steps["write_status"] == 1


async def test_never_runs_ends_in_alarm_row(
    env: WorkflowEnvironment, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_INSTALLED", "0")
    monkeypatch.setenv("FAKE_RUNNING_AFTER", "0")
    async with _worker(env.client):
        handle = await _start(env.client, _plan(max_readings=3))
        result = ModelProveRunningResult.model_validate_json(str(await handle.result()))
    assert result.outcome is EnumProveRunningOutcome.ALARM
    assert result.readings == 3
    assert result.installed_by_workflow is True
    rows = files["ledger"].read_text().splitlines()
    assert len(rows) == 1
    row = rows[0]
    assert "alarm=prove-running-deadline" in row
    assert "pr=omnimarket#6090" in row
    assert "sha=d2985cccba1234567890" in row
    assert "last_reading=running=False installed=False on dev-runtime" in row
    assert "reading 3" in row
    assert "STATUS | " in row
    assert "installed=dev-runtime:" not in row
    steps = _steps(files["log"])
    assert steps["install"] == 1
    assert [steps[f"read_running/{n}"] for n in (1, 2, 3)] == [1, 1, 1]
    assert steps["write_alarm"] == 1
    assert steps["write_status"] == 0


async def test_never_runs_deadline_stops_readings_before_the_limit(
    env: WorkflowEnvironment, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RUNNING_AFTER", "0")
    async with _worker(env.client):
        handle = await _start(
            env.client, _plan(wait_seconds=300, max_readings=50, deadline_seconds=900)
        )
        result = ModelProveRunningResult.model_validate_json(str(await handle.result()))
    assert result.outcome is EnumProveRunningOutcome.ALARM
    assert result.readings == 3
    assert "alarm=prove-running-deadline" in files["ledger"].read_text()


async def test_duplicate_start_joins_then_refuses_and_steps_run_once(
    env: WorkflowEnvironment, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_INSTALLED", "0")
    monkeypatch.setenv("FAKE_RUNNING_AFTER", "1")
    async with _worker(env.client):
        first = await _start(env.client, _plan())
        second = await _start(env.client, _plan())
        assert second.result_run_id == first.result_run_id
        await first.result()
        with pytest.raises(WorkflowAlreadyStartedError):
            await _start(env.client, _plan())
    steps = _steps(files["log"])
    assert steps["read_status"] == 1
    assert steps["install"] == 1
    assert steps["read_running/1"] == 1
    assert steps["write_status"] == 1
    assert len(files["ledger"].read_text().splitlines()) == 1


async def test_worker_restart_mid_wait_runs_no_step_twice(
    env: WorkflowEnvironment, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_INSTALLED", "0")
    monkeypatch.setenv("FAKE_RUNNING_AFTER", "2")
    async with _worker(env.client, cached=False):
        handle = await _start(env.client, _plan(wait_seconds=600))
        for _ in range(200):
            if _steps(files["log"])["install"] == 1:
                break
            await asyncio.sleep(0.1)
        # Let the workflow task that schedules the durable timer complete before the worker goes away.
        for _ in range(100):
            history = await handle.fetch_history()
            if any(
                e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
                for e in history.events
            ):
                break
            await asyncio.sleep(0.1)
    assert _steps(files["log"]) == Counter({"read_status": 1, "install": 1})
    # A new worker (the old one is gone): the timer fires on the server and the run resumes from its history.
    async with _worker(env.client, cached=False):
        result = ModelProveRunningResult.model_validate_json(str(await handle.result()))
    assert result.outcome is EnumProveRunningOutcome.PROVED
    steps = _steps(files["log"])
    assert steps["read_status"] == 1
    assert steps["install"] == 1
    assert steps["read_running/1"] == 1
    assert steps["read_running/2"] == 1
    assert steps["write_status"] == 1
    assert max(steps.values()) == 1


async def test_activities_call_nodes(
    env: WorkflowEnvironment, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_INSTALLED", "0")
    monkeypatch.setenv("FAKE_RUNNING_AFTER", "1")
    async with _worker(env.client):
        handle = await _start(env.client, _plan())
        await handle.result()
        history = await handle.fetch_history()
    scheduled = [
        e.activity_task_scheduled_event_attributes
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    assert {a.activity_type.name for a in scheduled} == {ACTIVITY_NAME}
    # Every step the workflow ordered ran as a node process that recorded its key; an inlined step records nothing.
    expected = {"read_status", "install", "read_running/1", "write_status"}
    assert set(_steps(files["log"])) == expected
    assert len(scheduled) == len(expected)
    # The workflow body runs no process and touches no file or network itself.
    body = inspect.getsource(ProveRunningWorkflow)
    for forbidden in (
        "subprocess",
        "open(",
        "os.",
        "socket",
        "requests",
        "httpx",
        "run_step_process",
    ):
        assert forbidden not in body, forbidden
    assert "run_step_process" in inspect.getsource(wf_module.prove_running_step)


async def test_activities_call_nodes_contract_steps_name_node_commands() -> None:
    from omnimarket.nodes.node_prove_running_workflow.runtime_prove_running import (
        contract_config,
    )

    steps = contract_config()["steps"]
    assert set(steps) == set(ModelProveRunningSteps.model_fields)
    for argv in steps.values():
        assert argv[:3] == ["{python}", "-m", NODE] or argv[0].startswith("onex"), argv


def test_unique_ids_per_sha() -> None:
    other = CHANGE.model_copy(update={"merge_sha": uuid.uuid4().hex[:12]})
    assert other.workflow_id != CHANGE.workflow_id
