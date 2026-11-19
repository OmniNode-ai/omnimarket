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

from omnimarket.handlers.floor_projection import InMemorySnapshotSource
from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionReadOverlay,
)
from omnimarket.models.integration_reading import (
    EnumIntegrationGapClass,
    EnumIntegrationState,
    ModelIntegrationReading,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_integration_projection_compute.handlers.handler_integration_projection import (
    HandlerIntegrationProjection,
)
from omnimarket.nodes.node_prove_running_workflow import (
    runtime_prove_running as runtime,
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

TOPIC = "onex.snapshot.projection.floor-integration.v1"
PRODUCER = "node_integration_reading_publish_effect:test-pass"


def _reading(
    number: int,
    target: str,
    merged: datetime,
    state: EnumIntegrationState = EnumIntegrationState.NOT_RUNNING,
) -> ModelIntegrationReading:
    running = state is EnumIntegrationState.RUNNING
    return ModelIntegrationReading(
        repository="omnimarket",
        number=number,
        title=f"Change {number}",
        merged_at=merged,
        target=target,
        state=state,
        gap_class=(
            EnumIntegrationGapClass.NONE
            if running
            else EnumIntegrationGapClass.NEEDS_UNIT
        ),
        gap="" if running else "unit missing",
        step="" if running else "install it",
        running_path="some_unit" if running else "",
        running_line="SEEN_RUNNING line" if running else "",
        read_at=merged,
        origin="test-pass",
    )


def publication(as_of: datetime, *, producer: str = PRODUCER) -> bytes:
    """One publication of the integration projection: three NOT RUNNING lines, one running, one more line later."""
    merged = datetime(2026, 11, 16, 9, tzinfo=UTC)
    readings = (
        _reading(42, "worker on h053", merged + timedelta(minutes=1)),
        _reading(41, "worker on H051", merged),
        _reading(43, "dev runtime", merged + timedelta(minutes=2)),
        _reading(
            44,
            "worker on h202",
            merged + timedelta(minutes=3),
            EnumIntegrationState.RUNNING,
        ),
    )
    projection = HandlerIntegrationProjection().handle(
        ModelIntegrationReadingBatch(as_of=as_of, origin="test-pass", readings=readings)
    )
    return (
        ModelFloorProjectionEnvelope(
            projection_id="integration",
            as_of=as_of,
            producer=producer,
            payload=projection.model_dump(mode="json"),
        )
        .model_dump_json()
        .encode()
    )


def _declare_projection(
    monkeypatch: pytest.MonkeyPatch, held: dict[str, list[bytes]]
) -> None:
    """The declared overlay and the held publications, in place of the broker (the stand-in of the unit's overlay)."""
    monkeypatch.setattr(
        runtime,
        "snapshot_source",
        lambda *_args: InMemorySnapshotSource(held),
    )
    monkeypatch.setattr(
        runtime,
        "projection_overlay",
        lambda *_args: ModelFloorProjectionReadOverlay.model_validate(
            {
                "bus": "inmemory",
                "read_window_s": 1.0,
                "projections": {"integration": {"topic": TOPIC, "max_age_s": 7800}},
            }
        ),
    )


HOSTS = ("h051", "h053", "dev-runtime")
WORKFLOW_IDS = tuple(f"omnimarket#{number}@{number:040x}" for number in (41, 42, 43))


@pytest.fixture
def batch_files(
    files: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    monkeypatch.delenv("ONEX_INTEGRATION_LEDGER_PATH", raising=False)
    published = tmp_path / "integration-publication.json"
    published.write_bytes(publication(datetime.now(UTC)))
    monkeypatch.setenv("FAKE_INTEGRATION_PUBLICATION", str(published))
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


def _read_step(monkeypatch: pytest.MonkeyPatch, limit: int = 3) -> None:
    step = ModelProveRunningBatchStepInput(
        step="read_not_running",
        step_key="batch/read_not_running",
        batch_id="batch",
        limit=limit,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(step.model_dump_json()))
    monkeypatch.setenv(
        "ONEX_PROVE_RUNNING_MERGE_SHA_ARGV",
        json.dumps([sys.executable, FAKE, "merge-sha"]),
    )


def test_read_not_running_reads_the_declared_batch_with_no_env_set_by_hand(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_INTEGRATION_LEDGER_PATH", raising=False)
    _declare_projection(monkeypatch, {TOPIC: [publication(datetime.now(UTC))]})
    _read_step(monkeypatch)
    assert runtime.main(["read-not-running"]) == 0
    selected = ModelProveRunningStartBatch.model_validate_json(capsys.readouterr().out)
    assert [c.number for c in selected.changes] == [41, 42, 43]
    assert [c.target for c in selected.changes] == [
        "worker-on-H051",
        "worker-on-h053",
        "dev-runtime",
    ]


def test_read_not_running_takes_the_newest_publication(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime.now(UTC)
    _declare_projection(
        monkeypatch,
        {
            TOPIC: [
                publication(now),
                publication(now - timedelta(minutes=30), producer="older"),
            ]
        },
    )
    _read_step(monkeypatch, limit=1)
    assert runtime.main(["read-not-running"]) == 0
    selected = ModelProveRunningStartBatch.model_validate_json(capsys.readouterr().out)
    assert [c.number for c in selected.changes] == [41]


def test_read_not_running_refuses_a_stale_batch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = datetime.now(UTC) - timedelta(hours=13)
    _declare_projection(monkeypatch, {TOPIC: [publication(stale)]})
    _read_step(monkeypatch)
    assert runtime.main(["read-not-running"]) == runtime.EXIT_REFUSED
    output = capsys.readouterr()
    assert "over the declared 7800 s" in output.err
    assert "the integration pass has published no current batch" in output.err
    assert not output.out


def test_read_not_running_refuses_a_topic_with_no_batch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _declare_projection(monkeypatch, {})
    _read_step(monkeypatch)
    assert runtime.main(["read-not-running"]) == runtime.EXIT_REFUSED
    output = capsys.readouterr()
    assert f"{TOPIC} holds no valid publication of integration" in output.err
    assert not output.out


def test_input_is_declared_in_the_contract_not_in_an_env_var() -> None:
    cfg = runtime.contract_config()
    assert "ledger_path_env" not in cfg["batch"]
    assert cfg["batch"]["input"]["projection_id"] == "integration"
    contract = (
        Path(runtime.__file__).with_name("contract.yaml").read_text(encoding="utf-8")
    )
    assert "ONEX_INTEGRATION_LEDGER_PATH" not in contract
    assert TOPIC in contract


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
    monkeypatch.setattr(runtime, "batch_hosts", lambda *_args: ("h051", "h053"))
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
