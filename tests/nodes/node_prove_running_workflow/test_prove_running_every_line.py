# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every NOT RUNNING line of the batch is read and gets one child; the only bounds are declared batching sizes."""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.testing import WorkflowEnvironment

from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope
from omnimarket.models.integration_reading import (
    EnumIntegrationState,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_integration_projection_compute.handlers.handler_integration_projection import (
    HandlerIntegrationProjection,
)
from omnimarket.nodes.node_prove_running_workflow import (
    runtime_prove_running as runtime,
)
from omnimarket.nodes.node_prove_running_workflow.handlers import (
    HandlerProveRunningStarter,
)
from omnimarket.nodes.node_prove_running_workflow.handlers.temporal_prove_running_batch import (
    ProveRunningBatchWorkflow,
)
from omnimarket.nodes.node_prove_running_workflow.models import (
    ModelProveRunningBatchResult,
    ModelProveRunningBatchStepInput,
    ModelProveRunningStartBatch,
    ModelProveRunningStartRequest,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_batch import (
    PRODUCER,
    TOPIC,
    WORKFLOW_IDS,
    _declare_projection,
    _reading,
    _worker,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_batch import (
    batch_files as batch_files,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_starter import (
    BATCH as SMALL_BATCH,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_workflow import (
    FAKE,
    QUEUE,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_workflow import (
    env as env,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_workflow import (
    files as files,
)

pytestmark = pytest.mark.unit

MERGED = datetime(2026, 11, 16, 9, tzinfo=UTC)


class _Resolver:
    """Answers every change but the missing ones."""

    def __init__(self, missing: frozenset[str] = frozenset()) -> None:
        self._missing = missing

    def merge_sha(self, repository: str, number: int) -> str | None:
        if f"{repository}#{number}" in self._missing:
            return None
        return f"{number:07d}abcdef"


def _many(count: int) -> bytes:
    """One publication of the integration projection with ``count`` NOT RUNNING lines and one running line."""
    as_of = datetime.now(UTC)
    not_running = [
        _reading(1000 + n, f"worker on h05{n % 3}", MERGED + timedelta(minutes=n))
        for n in range(count)
    ]
    running = _reading(
        9999,
        "worker on h202",
        MERGED + timedelta(minutes=count + 1),
        EnumIntegrationState.RUNNING,
    )
    readings = (*not_running, running)
    projection = HandlerIntegrationProjection().handle(
        ModelIntegrationReadingBatch(as_of=as_of, origin="test-pass", readings=readings)
    )
    return (
        ModelFloorProjectionEnvelope(
            projection_id="integration",
            as_of=as_of,
            producer=PRODUCER,
            payload=projection.model_dump(mode="json"),
        )
        .model_dump_json()
        .encode()
    )


def test_starter_with_no_limit_selects_every_not_running_line() -> None:
    selected = HandlerProveRunningStarter(_Resolver()).handle(
        ModelProveRunningStartRequest(batch=SMALL_BATCH)
    )
    assert [c.number for c in selected.changes] == [5990, 6003, 6002, 6004]
    assert selected.skipped == ()


def test_starter_resolves_with_the_declared_workers_and_keeps_oldest_first() -> None:
    resolver = _Resolver(missing=frozenset({"omnimarket#6003"}))
    selected = HandlerProveRunningStarter(resolver, workers=4).handle(
        ModelProveRunningStartRequest(batch=SMALL_BATCH)
    )
    assert [c.number for c in selected.changes] == [5990, 6002, 6004]
    assert selected.skipped == ("omnimarket#6003: merge sha unread",)


def test_contract_declares_batching_sizes_and_no_selection_cap() -> None:
    cfg = runtime.contract_config()
    assert "limit" not in cfg["batch"]
    assert int(cfg["batch"]["child_batch_size"]) >= 1
    assert int(cfg["batch"]["merge_sha_workers"]) >= 1
    plan = runtime.build_batch_plan(cfg)
    assert plan.child_batch_size == int(cfg["batch"]["child_batch_size"])


def test_read_not_running_reads_every_line_of_a_large_batch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _declare_projection(monkeypatch, {TOPIC: [_many(60)]})
    step = ModelProveRunningBatchStepInput(
        step="read_not_running",
        step_key="batch/read_not_running",
        batch_id="batch",
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(step.model_dump_json()))
    monkeypatch.setenv(
        "ONEX_PROVE_RUNNING_MERGE_SHA_ARGV",
        json.dumps([sys.executable, FAKE, "merge-sha"]),
    )
    assert runtime.main(["read-not-running"]) == 0
    selected = ModelProveRunningStartBatch.model_validate_json(capsys.readouterr().out)
    assert len(selected.changes) == 60
    assert selected.skipped == ()
    assert [c.number for c in selected.changes] == [1000 + n for n in range(60)]


async def test_children_start_in_declared_batches_and_every_change_gets_one(
    env: WorkflowEnvironment, batch_files: dict[str, Path]
) -> None:
    cfg = runtime.contract_config()
    cfg["batch"]["child_batch_size"] = 1
    for name, argv in cfg["batch"]["steps"].items():
        cfg["batch"]["steps"][name] = [argv[0], FAKE, "batch-step", argv[-1]]
    batch_id = "batch-sized"
    async with _worker(env.client):
        handle = await env.client.start_workflow(
            ProveRunningBatchWorkflow.run,
            runtime.build_batch_plan(cfg).model_dump_json(),
            id=batch_id,
            task_queue=QUEUE,
        )
        result = ModelProveRunningBatchResult.model_validate_json(await handle.result())
        history = await handle.fetch_history()
    assert set(result.started) == set(WORKFLOW_IDS)
    assert result.outcomes == dict.fromkeys(WORKFLOW_IDS, "proved")
    marks = [
        "start"
        if e.event_type == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
        else "done"
        for e in history.events
        if e.event_type
        in (
            EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED,
            EventType.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_COMPLETED,
        )
    ]
    assert marks == ["start", "done"] * 3
    rows = batch_files["ledger"].read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3
