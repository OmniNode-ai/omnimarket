# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The worktree prune workflow on Temporal's time-skipping test server, its steps real processes: per host one pruner
step and one report step, a failing pruner retried then reported failed with the run going on, a disk alarm past the
bar, and a second start for the same closed ticket refused."""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from omnimarket.nodes.node_worktree_prune_temporal_effect.handlers.handler_prune_report import (
    HandlerPruneReport,
)
from omnimarket.nodes.node_worktree_prune_temporal_effect.handlers.temporal_worktree_prune import (
    STEP_ACTIVITY,
    WORKFLOW_NAME,
    WorktreePruneWorkflow,
    worktree_prune_step,
)
from omnimarket.nodes.node_worktree_prune_temporal_effect.models import (
    ModelPruneHostTarget,
    ModelPruneReportRequest,
    ModelWorktreePrunePlan,
    ModelWorktreePruneRunResult,
)

pytestmark = pytest.mark.unit

QUEUE = "worktree-prune-test"
NODE_MODULE = "omnimarket.nodes.node_worktree_prune_temporal_effect"


def _pruner(tmp_path: Path, name: str, *, fail_first: int = 0) -> tuple[str, ...]:
    """A pruner stand-in: counts its runs in a file, fails the first ``fail_first``, then prints the completed event."""
    counter = tmp_path / f"{name}.runs"
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json, pathlib, sys\n"
        f"c = pathlib.Path({str(counter)!r})\n"
        "n = int(c.read_text()) + 1 if c.exists() else 1\n"
        "c.write_text(str(n))\n"
        f"if n <= {fail_first}:\n"
        "    sys.stderr.write('pruner unavailable')\n"
        "    sys.exit(3)\n"
        "print(json.dumps({'completed_event': {'scanned': 13, 'removed': 2, 'pinned_and_removed': 1, 'kept': 10,"
        " 'needs_human': 0, 'failures': 0, 'freed_bytes': 4096}}))\n"
    )
    return (sys.executable, str(script))


def _runs(tmp_path: Path, name: str) -> int:
    counter = tmp_path / f"{name}.runs"
    return int(counter.read_text()) if counter.exists() else 0


def _plan(hosts: list[ModelPruneHostTarget], **kw: object) -> str:
    base: dict[str, object] = {
        "trigger": "manual",
        "hosts": hosts,
        "retry_initial_seconds": 1,
        "max_attempts": 3,
        "report_argv": (sys.executable, "-m", NODE_MODULE, "report"),
    }
    base.update(kw)
    return ModelWorktreePrunePlan.model_validate(base).model_dump_json()


def _host(name: str, argv: tuple[str, ...], bar: float) -> ModelPruneHostTarget:
    return ModelPruneHostTarget(
        host=name,
        task_queue=QUEUE,
        pruner_argv=argv,
        disk_path="/",
        disk_bar_percent=bar,
    )


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


def _worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=QUEUE,
        workflows=[WorktreePruneWorkflow],
        activities=[worktree_prune_step],
    )


async def _step_count(client: Client, wf_id: str) -> int:
    n = 0
    async for event in client.get_workflow_handle(wf_id).fetch_history_events():
        if (
            event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
            and event.activity_task_scheduled_event_attributes.activity_type.name
            == STEP_ACTIVITY
        ):
            n += 1
    return n


async def test_activities_call_nodes_one_pruner_and_one_report_step_per_host(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    hosts = [
        _host("h-a", _pruner(tmp_path, "a"), 100.0),
        _host("h-b", _pruner(tmp_path, "b"), 100.0),
    ]
    wf_id = f"worktree-prune-{uuid.uuid4()}"
    async with _worker(env.client):
        raw = await env.client.execute_workflow(
            WORKFLOW_NAME, _plan(hosts), id=wf_id, task_queue=QUEUE
        )
    result = ModelWorktreePruneRunResult.model_validate_json(raw)
    assert [r.host for r in result.rows] == ["h-a", "h-b"]
    assert all(r.status == "ok" for r in result.rows)
    assert [(r.scanned, r.removed, r.kept, r.freed_bytes) for r in result.rows] == [
        (13, 3, 10, 4096)
    ] * 2
    assert result.freed_bytes == 8192
    assert [r.step_key for r in result.rows] == [
        f"{wf_id}/h-a/report",
        f"{wf_id}/h-b/report",
    ]
    # the pruner ran once per host; two hosts, two steps each
    assert (_runs(tmp_path, "a"), _runs(tmp_path, "b")) == (1, 1)
    assert await _step_count(env.client, wf_id) == 4


async def test_failing_pruner_is_retried_then_the_run_goes_on(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    hosts = [
        _host("h-bad", _pruner(tmp_path, "bad", fail_first=99), 100.0),
        _host("h-flaky", _pruner(tmp_path, "flaky", fail_first=1), 100.0),
    ]
    async with _worker(env.client):
        raw = await env.client.execute_workflow(
            WORKFLOW_NAME,
            _plan(hosts),
            id=f"worktree-prune-{uuid.uuid4()}",
            task_queue=QUEUE,
        )
    result = ModelWorktreePruneRunResult.model_validate_json(raw)
    bad, flaky = result.rows
    assert bad.status == "failed"
    assert "exited 3" in (bad.error or "")
    assert _runs(tmp_path, "bad") == 3  # the plan's attempt limit
    assert flaky.status == "ok"
    assert flaky.removed == 3
    assert _runs(tmp_path, "flaky") == 2  # one retry of the same step


async def test_disk_alarm_when_use_is_past_the_hosts_bar(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    hosts = [
        _host("h-over", _pruner(tmp_path, "over"), 0.01),
        _host("h-under", _pruner(tmp_path, "under"), 100.0),
    ]
    async with _worker(env.client):
        raw = await env.client.execute_workflow(
            WORKFLOW_NAME,
            _plan(hosts),
            id=f"worktree-prune-{uuid.uuid4()}",
            task_queue=QUEUE,
        )
    result = ModelWorktreePruneRunResult.model_validate_json(raw)
    over, under = result.rows
    assert over.alarm is not None
    assert over.alarm.host == "h-over"
    assert over.alarm.bar_percent == 0.01
    assert over.alarm.used_percent > 0.01
    assert under.alarm is None
    assert [a.host for a in result.alarms] == ["h-over"]


async def test_duplicate_start_for_the_same_closed_ticket_is_refused(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    hosts = [_host("h-a", _pruner(tmp_path, "dup"), 100.0)]
    wf_id = "worktree-prune-ticket-OMN-1"
    async with _worker(env.client):
        await env.client.execute_workflow(
            WORKFLOW_NAME,
            _plan(hosts, trigger="ticket_closed", ticket="OMN-1"),
            id=wf_id,
            task_queue=QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        with pytest.raises((WorkflowAlreadyStartedError, WorkflowFailureError)):
            await env.client.start_workflow(
                WORKFLOW_NAME,
                _plan(hosts, trigger="ticket_closed", ticket="OMN-1"),
                id=wf_id,
                task_queue=QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
    assert _runs(tmp_path, "dup") == 1


def _request(**kw: object) -> ModelPruneReportRequest:
    base: dict[str, object] = {
        "step_key": "wf/h/report",
        "host": "h",
        "disk_path": "/",
        "disk_bar_percent": 90.0,
    }
    base.update(kw)
    return ModelPruneReportRequest.model_validate(base)


def test_report_reads_the_completed_event_or_the_bare_counts() -> None:
    handler = HandlerPruneReport(disk_reader=lambda _p: 40.0)
    nested = handler.handle(
        _request(
            pruner_output=json.dumps({"completed_event": {"scanned": 5, "kept": 5}})
        )
    )
    bare = handler.handle(_request(pruner_output=json.dumps({"scanned": 5, "kept": 5})))
    assert (nested.scanned, nested.kept, nested.status) == (5, 5, "ok")
    assert (bare.scanned, bare.kept) == (5, 5)
    assert nested.disk_used_percent == 40.0
    assert nested.alarm is None


def test_report_failed_pruner_or_unreadable_output_is_a_failed_row_without_an_alarm() -> (
    None
):
    handler = HandlerPruneReport(disk_reader=lambda _p: 99.0)
    failed = handler.handle(_request(error="pruner exited 3"))
    garbled = handler.handle(_request(pruner_output="not json"))
    assert (failed.status, failed.error, failed.alarm) == (
        "failed",
        "pruner exited 3",
        None,
    )
    assert garbled.status == "failed"
    assert "unreadable pruner output" in (garbled.error or "")
    assert garbled.alarm is None


def test_report_alarm_is_strictly_past_the_bar() -> None:
    at_bar = HandlerPruneReport(disk_reader=lambda _p: 90.0).handle(
        _request(pruner_output="{}")
    )
    past = HandlerPruneReport(disk_reader=lambda _p: 90.01).handle(
        _request(pruner_output="{}")
    )
    assert at_bar.alarm is None
    assert past.alarm is not None
    assert past.alarm.used_percent == 90.01


def test_first_fire_offset_puts_the_first_fire_where_asked_and_keeps_the_cadence() -> (
    None
):
    from datetime import UTC, datetime, timedelta

    from omnimarket.nodes.node_worktree_prune_temporal_effect.runtime_worktree_prune import (
        first_fire_offset,
    )

    every = timedelta(hours=6)
    now = datetime(2026, 11, 18, 5, 50, tzinfo=UTC)
    offset = first_fire_offset(every, timedelta(minutes=10), now)
    # fires are offset + k * every counted from the epoch: 06:00 is the first one after 05:50
    first = datetime(2026, 11, 18, 6, 0, tzinfo=UTC).timestamp()
    assert (first - offset.total_seconds()) % every.total_seconds() == 0
