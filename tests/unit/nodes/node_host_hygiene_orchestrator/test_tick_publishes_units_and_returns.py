# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime-tick route publishes each due lab work unit and returns; the receipts settle the run.

A tick handler that blocks until the hosts answer holds a runtime dispatch for the sum of the units'
timeouts (read 2026-11-25T01:24Z: one orphaned dispatch per hour, 8100 s each, against an orphan limit
of 3). The route under test sends nothing itself: it puts each unit on the lab work command topic and
returns, and the receipt handlers read the terminals the hosts publish and emit the run when the last
unit has answered.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.lab_work.bus import ModelLabWorkCommandFailure
from omnimarket.models.model_host_disk_reading import (
    ModelDiskClassReading,
    ModelHostDiskRead,
    ModelHostDiskVolume,
)
from omnimarket.models.model_host_hygiene import EnumHostHygieneReason
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneRunOutcome,
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneOrchestratorOverlay,
    ModelHostHygieneRunCompleted,
)
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    lab_work_command_topic,
    run_completed_topic,
    schedule_run_topic,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.handler_host_hygiene_receipt import (
    HandlerHostHygieneUnitCompleted,
    HandlerHostHygieneUnitFailed,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.handler_host_hygiene_scheduled import (
    NODE_NAME,
    HandlerHostHygieneScheduled,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_disk_readings import (
    HostDiskReadingStore,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.pending_runs import (
    PendingHostHygieneRuns,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes" / NODE_NAME
)
T0 = dt.datetime(2026, 11, 25, 1, 9, 10, tzinfo=dt.UTC)


def _overlay() -> ModelHostHygieneOrchestratorOverlay:
    data = yaml.safe_load(next(NODE_DIR.glob("*/lab_work.example.yaml")).read_text())
    return ModelHostHygieneOrchestratorOverlay.model_validate(data)


def _tick(now: dt.datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid.uuid4(),
        scheduler_id="test",
        tick_interval_ms=1000,
    )


class _BlockingSender:
    """A sender whose send waits for a host that never answers."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.sent: list[ModelLabWorkUnitRequest] = []

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        self.sent.append(request)
        self.release.wait(30)
        raise TimeoutError("host never answered")


def _store_under_pressure() -> HostDiskReadingStore:
    overlay = _overlay()
    store = HostDiskReadingStore(max_hosts=8)
    store.record_read(
        ModelHostDiskRead(
            correlation_id="c1",
            host_name=overlay.hosts[0].name,
            read_at=T0 - dt.timedelta(seconds=30),
            volumes=(
                ModelHostDiskVolume(
                    path=overlay.hosts[0].pressure_volume,
                    total_bytes=100,
                    free_bytes=10,
                    used_bytes=90,
                ),
            ),
            classes=(
                ModelDiskClassReading(
                    class_name="uv_cache", status="unread", unread_reason="not read"
                ),
            ),
        )
    )
    return store


def _handlers(
    sender: Any = None,
) -> tuple[
    HandlerHostHygieneScheduled,
    HandlerHostHygieneUnitCompleted,
    HandlerHostHygieneUnitFailed,
    PendingHostHygieneRuns,
]:
    overlay = _overlay()
    pending = PendingHostHygieneRuns()

    def clock() -> dt.datetime:
        return T0 + dt.timedelta(seconds=5)

    common: dict[str, Any] = {
        "gate": ScheduleGate(NODE_NAME, declaration=overlay),
        "store": _store_under_pressure(),
        "pending": pending,
        "clock": clock,
    }
    return (
        HandlerHostHygieneScheduled(sender=sender, **common),
        HandlerHostHygieneUnitCompleted(**common),
        HandlerHostHygieneUnitFailed(**common),
        pending,
    )


def _receipt(
    request: ModelLabWorkUnitRequest,
    status: EnumLabWorkUnitStatus,
    exit_code: int | None,
) -> ModelLabWorkUnitReceipt:
    return ModelLabWorkUnitReceipt(
        work_unit_id=request.work_unit_id,
        target_host=request.target_host,
        host=request.target_host,
        repo=request.repo,
        commit_sha=request.commit_sha,
        lane=request.lane,
        kind=request.kind,
        status=status,
        exit_code=exit_code,
        log_path="x",
        output_tail="ok",
        duration_seconds=1.0,
    )


def _commands(output: Any) -> list[ModelLabWorkUnitRequest]:
    return [
        ModelLabWorkUnitRequest.model_validate(e.payload)
        for e in output.events
        if e.event_type == lab_work_command_topic()
    ]


async def test_the_tick_publishes_every_due_unit_and_returns_without_waiting_for_a_host() -> (
    None
):
    sender = _BlockingSender()
    scheduled, _, _, _ = _handlers(sender)
    try:
        # A blocked host must not hold the tick: a tick that waits here is the orphaned dispatch.
        output = await asyncio.wait_for(scheduled.handle(_tick(T0)), timeout=5)
    finally:
        sender.release.set()

    commands = _commands(output)
    overlay = _overlay()
    assert (
        len(commands) == 2
    )  # the example host's two cleanups, both due on an unread host
    assert sender.sent == []
    assert {c.target_host for c in commands} == {overlay.hosts[0].name}
    # Not a run event yet: the run is only known once the hosts have answered.
    assert [
        e.event_type for e in output.events if e.event_type == run_completed_topic()
    ] == []


async def test_the_last_receipt_settles_the_run_and_the_schedule_event() -> None:
    scheduled, completed, _, _ = _handlers()
    output = await scheduled.handle(_tick(T0))
    commands = _commands(output)
    assert commands

    last = None
    for request in commands:
        last = await completed.handle(
            _receipt(request, EnumLabWorkUnitStatus.COMPLETED, 0)
        )
        if request is not commands[-1]:
            assert last.events == ()

    assert last is not None
    run_env, schedule_env = last.events
    assert run_env.event_type == run_completed_topic()
    assert schedule_env.event_type == schedule_run_topic()
    run = run_env.payload
    assert isinstance(run, ModelHostHygieneRunCompleted)
    assert isinstance(schedule_env.payload, ModelScheduleRunEvent)
    assert run.outcome is EnumHostHygieneRunOutcome.COMPLETED
    assert {u.work_unit_id for u in run.units} == {c.work_unit_id for c in commands}
    assert {i.reason for i in run.decided} <= set(EnumHostHygieneReason)
    assert schedule_env.payload.outcome == "completed"


async def test_a_unit_that_ran_waits_its_interval_and_a_failure_terminal_is_sent_again() -> (
    None
):
    scheduled, completed, failed, pending = _handlers()
    output = await scheduled.handle(_tick(T0))
    commands = _commands(output)
    ran, lost = commands[0], commands[1]
    await completed.handle(_receipt(ran, EnumLabWorkUnitStatus.COMPLETED, 1))
    failure = ModelLabWorkCommandFailure(
        work_unit_id=lost.work_unit_id, host=lost.target_host, error_message="boom"
    )
    settled = None
    for request in commands[2:]:
        await completed.handle(_receipt(request, EnumLabWorkUnitStatus.COMPLETED, 0))
    settled = await failed.handle(failure)

    run = settled.events[0].payload
    assert isinstance(run, ModelHostHygieneRunCompleted)
    outcomes = {u.work_unit_id: u.outcome for u in run.units}
    assert outcomes[ran.work_unit_id] is EnumHostHygieneUnitOutcome.FAILED
    assert (
        outcomes[lost.work_unit_id] is EnumHostHygieneUnitOutcome.TRANSPORT_UNAVAILABLE
    )
    assert run.outcome is EnumHostHygieneRunOutcome.FAILED
    # The unit that ran is remembered with the tick time; the one that never reached its host is not.
    assert len(pending.dispatched) == len(commands) - 1


async def test_a_receipt_for_a_unit_this_node_did_not_send_is_ignored() -> None:
    _, completed, failed, _ = _handlers()
    stranger = ModelLabWorkUnitRequest(
        work_unit_id="other-0001",
        target_host="example-host-a",
        repo="example-owner/example-repo",
        commit_sha="0" * 40,
        argv=["true"],
        timeout_seconds=1,
        lane="example-lane",
    )

    out_a = await completed.handle(
        _receipt(stranger, EnumLabWorkUnitStatus.COMPLETED, 0)
    )
    out_b = await failed.handle(
        ModelLabWorkCommandFailure(work_unit_id="other-0001", error_message="x")
    )

    assert out_a.events == ()
    assert out_b.events == ()


async def test_a_run_whose_hosts_never_answer_is_settled_by_a_later_tick() -> None:
    scheduled, _, _, pending = _handlers()
    await scheduled.handle(_tick(T0))
    assert pending.open_runs() == 1
    overlay = _overlay()
    longest = max(c.timeout_seconds for c in overlay.cleanups)

    later = T0 + dt.timedelta(seconds=overlay.run_interval_seconds + longest + 3600)
    output = await scheduled.handle(_tick(later))

    runs = [
        e.payload
        for e in output.events
        if e.event_type == run_completed_topic()
        and isinstance(e.payload, ModelHostHygieneRunCompleted)
    ]
    assert len(runs) == 1
    assert {u.outcome for u in runs[0].units} == {
        EnumHostHygieneUnitOutcome.TRANSPORT_UNAVAILABLE
    }
