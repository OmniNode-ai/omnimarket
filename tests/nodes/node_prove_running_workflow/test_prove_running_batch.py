# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hourly batch orchestration with real node subprocesses and a time-skipping Temporal server."""

from __future__ import annotations

import inspect
import io
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, ScheduleActionStartWorkflow, ScheduleOverlapPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from omnimarket.models.integration_reading import (
    EnumIntegrationGapClass,
    EnumIntegrationState,
)
from omnimarket.nodes.node_prove_running_workflow import (
    runtime_prove_running as runtime,
)
from omnimarket.nodes.node_prove_running_workflow.handlers.handler_integration_ledger_reader import (
    read_not_running,
)
from omnimarket.nodes.node_prove_running_workflow.handlers.temporal_prove_running import (
    ProveRunningWorkflow,
    prove_running_step,
)
from omnimarket.nodes.node_prove_running_workflow.handlers.temporal_prove_running_batch import (
    BATCH_ACTIVITY_NAME,
    ProveRunningBatchWorkflow,
    prove_running_batch_step,
)
from omnimarket.nodes.node_prove_running_workflow.models import (
    ModelProveRunningBatchResult,
    ModelProveRunningBatchStepInput,
    ModelProveRunningChange,
    ModelProveRunningHostGroups,
    ModelProveRunningStartBatch,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_workflow import (
    FAKE,
    NODE,
    QUEUE,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_workflow import (
    env as env,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_workflow import (
    files as files,
)

pytestmark = pytest.mark.unit

LEDGER = """# Integration ledger
## NOT RUNNING (4, oldest merge first)
- omnimarket 42 | Second | merged 2026-11-16 09:01Z | runs: worker on h053 | state: NOT RUNNING | gap: unit missing | step: install it | owner: NONE
- omnimarket 41 | First | merged 2026-11-16 09:00Z | runs: worker on H051 | state: NOT RUNNING | gap: unit missing | step: install it
- omnimarket (no number) | Overlay | merged 2026-11-16 08:59Z | runs: h202 | state: NOT RUNNING | gap: missing | step: read again
- omnimarket 43 | Third | merged 2026-11-16 09:02Z | runs: dev runtime | state: NOT RUNNING | gap: | step:
## SEEN RUNNING
- omnimarket 44 | Other section | merged 2026-11-16 08:58Z | runs: h202 | state: NOT RUNNING | gap: old | step: reread
"""
HOSTS = ("h051", "h053", "dev-runtime")
WORKFLOW_IDS = tuple(f"omnimarket#{number}@{number:040x}" for number in (41, 42, 43))


@pytest.fixture
def batch_files(
    files: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    integration = tmp_path / "integration.md"
    integration.write_text(LEDGER, encoding="utf-8")
    monkeypatch.setenv("ONEX_INTEGRATION_LEDGER_PATH", str(integration))
    monkeypatch.setenv(
        "ONEX_PROVE_RUNNING_MERGE_SHA_ARGV",
        json.dumps([sys.executable, FAKE, "merge-sha"]),
    )
    monkeypatch.setenv("FAKE_INSTALLED", "0")
    monkeypatch.setenv("FAKE_RUNNING_AFTER", "1")
    return files


def _worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=QUEUE,
        workflows=[ProveRunningBatchWorkflow, ProveRunningWorkflow],
        activities=[prove_running_batch_step, prove_running_step],
    )


async def _run_batch(client: Client, batch_id: str) -> ModelProveRunningBatchResult:
    cfg = runtime.contract_config()
    # The stand-in logs the batch verb and invokes the real node CLI with test child commands.
    for name, argv in cfg["batch"]["steps"].items():
        cfg["batch"]["steps"][name] = [argv[0], FAKE, "batch-step", argv[-1]]
    handle = await client.start_workflow(
        ProveRunningBatchWorkflow.run,
        runtime.build_batch_plan(cfg).model_dump_json(),
        id=batch_id,
        task_queue=QUEUE,
    )
    return ModelProveRunningBatchResult.model_validate_json(await handle.result())


def test_schedule_hourly() -> None:
    cfg = runtime.contract_config()
    schedule = runtime.build_schedule(cfg, QUEUE)
    assert [interval.every for interval in schedule.spec.intervals] == [
        timedelta(hours=1)
    ]
    assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
    assert isinstance(schedule.action, ScheduleActionStartWorkflow)
    assert schedule.action.workflow == "ProveRunningBatchWorkflow"
    assert schedule.action.id == "onex-prove-running-hourly"
    assert schedule.action.task_queue == QUEUE

    plan = runtime.build_batch_plan(cfg)
    assert list(schedule.action.args) == [plan.model_dump_json()]
    assert plan.limit == 3
    assert plan.steps.read_not_running == (
        sys.executable,
        "-m",
        NODE,
        "read-not-running",
    )
    assert plan.steps.group_by_host == (sys.executable, "-m", NODE, "group-by-host")


def test_read_not_running_section() -> None:
    as_of = datetime(2026, 11, 16, 10, tzinfo=UTC)
    batch = read_not_running(LEDGER, as_of=as_of, origin="test")
    assert batch.as_of == as_of
    assert batch.origin == "test"
    assert [reading.number for reading in batch.readings] == [42, 41, 43]
    first, _, last = batch.readings
    assert first.title == "Second"
    assert first.merged_at == datetime(2026, 11, 16, 9, 1, tzinfo=UTC)
    assert first.read_at == as_of
    assert first.state is EnumIntegrationState.NOT_RUNNING
    assert first.gap_class is EnumIntegrationGapClass.NEEDS_READING
    assert first.target == "worker on h053"
    assert first.gap == "unit missing"
    assert first.step == "install it"
    assert last.gap == last.step == "none stated"


@pytest.mark.parametrize("missing", [None, "missing-ledger.md"])
def test_read_not_running_refuses_unreadable_ledger(
    missing: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_var = "ONEX_INTEGRATION_LEDGER_PATH"
    if missing is None:
        monkeypatch.delenv(env_var, raising=False)
    else:
        monkeypatch.setenv(env_var, str(tmp_path / missing))
    step = ModelProveRunningBatchStepInput(
        step="read_not_running",
        step_key="batch/read_not_running",
        batch_id="batch",
        limit=3,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(step.model_dump_json()))
    assert runtime.main(["read-not-running"]) == runtime.EXIT_REFUSED
    output = capsys.readouterr()
    assert env_var in output.err
    assert not output.out


def test_groups_by_host_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    batch = ModelProveRunningStartBatch(
        changes=tuple(
            ModelProveRunningChange(
                repository="omnimarket",
                number=number,
                merge_sha=f"{number:040x}",
                target=target,
            )
            for number, target in enumerate(
                ("h053-unit", "h202-or-H051-unit", "h0510-dev-runtime", "again-h051"),
                41,
            )
        ),
        skipped=("omnimarket#45: merge sha unread",),
    )
    step = ModelProveRunningBatchStepInput(
        step="group_by_host",
        step_key="batch/group_by_host",
        batch_id="batch",
        limit=4,
        payload=batch.model_dump_json(),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(step.model_dump_json()))
    assert runtime.main(["group-by-host"]) == 0
    groups = ModelProveRunningHostGroups.model_validate_json(capsys.readouterr().out)
    assert tuple(group.host for group in groups.groups) == HOSTS
    assert tuple(tuple(p.change.number for p in g.plans) for g in groups.groups) == (
        (42, 44),
        (41,),
        (43,),
    )
    assert groups.skipped == batch.skipped
    for group in groups.groups:
        for plan in group.plans:
            assert plan.change.target == group.host
            assert plan.batch_id == step.batch_id
            assert (plan.wait_seconds, plan.max_readings, plan.deadline_seconds) == (
                120,
                3,
                900,
            )


async def test_groups_by_host_batch(
    env: WorkflowEnvironment, batch_files: dict[str, Path]
) -> None:
    async with _worker(env.client):
        result = await _run_batch(env.client, "batch-groups")
    assert result.read_changes == 3
    assert result.hosts == dict.fromkeys(HOSTS, 1)
    assert set(result.started) == set(WORKFLOW_IDS)


async def test_activities_call_nodes(
    env: WorkflowEnvironment, batch_files: dict[str, Path]
) -> None:
    batch_id = "batch-activities"
    async with _worker(env.client):
        await _run_batch(env.client, batch_id)
        history = await env.client.get_workflow_handle(batch_id).fetch_history()

    scheduled = [
        e.activity_task_scheduled_event_attributes
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    steps = ["read-not-running", "group-by-host"]
    assert [a.activity_type.name for a in scheduled] == [BATCH_ACTIVITY_NAME] * len(
        steps
    )
    commands = [
        (await env.client.data_converter.decode(a.input.payloads))[0][-1]
        for a in scheduled
    ]
    assert commands == steps
    log = batch_files["log"].read_text(encoding="utf-8").splitlines()
    assert [line for line in log if line.split()[0] in steps] == [
        f"read-not-running {batch_id}/read_not_running",
        f"group-by-host {batch_id}/group_by_host",
    ]
    body = inspect.getsource(ProveRunningBatchWorkflow.run)
    for forbidden in ("subprocess", "open(", "json.loads"):
        assert forbidden not in body


async def test_one_status_row_per_change(
    env: WorkflowEnvironment, batch_files: dict[str, Path]
) -> None:
    batch_id = "batch-rows"
    async with _worker(env.client):
        result = await _run_batch(env.client, batch_id)
    assert result.batch_id == batch_id
    assert result.outcomes == dict.fromkeys(WORKFLOW_IDS, "proved")
    rows = batch_files["ledger"].read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3
    for wf_id, host in zip(WORKFLOW_IDS, HOSTS, strict=True):
        pr, sha = wf_id.split("@")
        (row,) = [row for row in rows if f" | pr={pr} | " in row]
        assert " | STATUS | " in row
        assert f"installed={host}:{sha[:12]}" in row
        assert "observed=dispatch seen in the runtime log" in row
        assert "read_at=2026-11-16T16:01:00Z" in row
        assert (
            f"workflow={wf_id} | step={wf_id}/write_status | batch={batch_id} | " in row
        )


async def test_refire_no_duplicate(
    env: WorkflowEnvironment, batch_files: dict[str, Path]
) -> None:
    async with _worker(env.client):
        await _run_batch(env.client, "batch-first")
        second = await _run_batch(env.client, "batch-second")

    assert set(second.already_proven_or_running) == set(WORKFLOW_IDS)
    assert second.started == ()
    assert second.outcomes == {}
    assert len(batch_files["ledger"].read_text(encoding="utf-8").splitlines()) == 3
    steps = Counter(batch_files["log"].read_text(encoding="utf-8").splitlines())
    for wf_id in WORKFLOW_IDS:
        assert steps[f"read {wf_id}/read_status"] == 1
        assert steps[f"install {wf_id}/install"] == 1
        assert steps[f"read {wf_id}/read_running/1"] == 1


def test_append_row_through_bus_command(
    batch_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no test override, a row goes through the bus append command the unit names, never a ledger file."""
    monkeypatch.delenv("ONEX_PROVE_RUNNING_LEDGER_ARGV", raising=False)
    monkeypatch.setenv("ONEX_RUN_STEP_KEY", "bus-append-step")
    monkeypatch.setenv(
        "ONEX_LEDGER_BUS_APPEND_COMMAND", f"{sys.executable} {FAKE} bus-append"
    )
    monkeypatch.setenv("ONEX_LEDGER_HOST_NAME", "h051")
    row = "2026-11-19T00:00:00Z | STATUS | lane=prove-running | pr=omnimarket#1 | read_at=2026-11-19T00:00:00Z"
    assert runtime._append_row(row, "wf/write_status", "prove-running") == (0, "")
    assert runtime._append_row(row, "wf/write_status", "prove-running") == (0, "")
    rows = batch_files["ledger"].read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2
    assert (
        rows[0] == rows[1]
    )  # one step key, one request id: the ledger host answers it once
    assert rows[0].startswith(row + " | req=")
    monkeypatch.delenv("ONEX_LEDGER_BUS_APPEND_COMMAND")
    code, error = runtime._append_row(row, "wf/write_status", "prove-running")
    assert code == 2
    assert "ONEX_LEDGER_BUS_APPEND_COMMAND" in error
