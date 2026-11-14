# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime-tick route of the host hygiene orchestrator (OMN-23707): golden, gate, error and failing-control cases.

The handler is built the way the runtime builds it, with the real schedule gate over the in-memory profile overlay, the real
reading store and an in-memory lab-work sender; only the sender's answers are scripted.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
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
from omnimarket.models.model_lab_work_unit import EnumLabWorkUnitStatus
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    run_completed_topic,
    schedule_run_topic,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.handler_host_hygiene_scheduled import (
    NODE_NAME,
    HandlerHostHygieneScheduled,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_disk_readings import (
    HostDiskReadingStore,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_in_memory import (
    InMemoryHostHygieneSender,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes" / NODE_NAME
)
T0 = dt.datetime(2026, 11, 13, 12, 0, tzinfo=dt.UTC)


def _overlay() -> ModelHostHygieneOrchestratorOverlay:
    data = yaml.safe_load((NODE_DIR / "overlays" / "in_memory.yaml").read_text())
    return ModelHostHygieneOrchestratorOverlay.model_validate(data)


def _tick(now: dt.datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid.uuid4(),
        scheduler_id="omn23707-test",
        tick_interval_ms=1000,
    )


def _store_with_reading(
    used_percent: int, read_at: dt.datetime
) -> HostDiskReadingStore:
    overlay = _overlay()
    store = HostDiskReadingStore(max_hosts=8)
    total = 100
    store.record_read(
        ModelHostDiskRead(
            correlation_id="c1",
            host_name=overlay.hosts[0].name,
            read_at=read_at,
            volumes=(
                ModelHostDiskVolume(
                    path=overlay.hosts[0].pressure_volume,
                    total_bytes=total,
                    free_bytes=total - used_percent,
                    used_bytes=used_percent,
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


def _handler(
    store: HostDiskReadingStore, sender: InMemoryHostHygieneSender
) -> HandlerHostHygieneScheduled:
    overlay = _overlay()
    return HandlerHostHygieneScheduled(
        gate=ScheduleGate(NODE_NAME, declaration=overlay),
        store=store,
        sender=sender,
        clock=lambda: T0 + dt.timedelta(seconds=5),
    )


def _payloads(
    output: Any,
) -> tuple[ModelHostHygieneRunCompleted, ModelScheduleRunEvent]:
    run, schedule = output.events
    assert run.event_type == run_completed_topic()
    assert schedule.event_type == schedule_run_topic()
    assert isinstance(run.payload, ModelHostHygieneRunCompleted)
    assert isinstance(schedule.payload, ModelScheduleRunEvent)
    return run.payload, schedule.payload


async def test_golden_pressure_dispatches_every_due_cleanup_and_reports_one_run() -> (
    None
):
    overlay = _overlay()
    sender = InMemoryHostHygieneSender()
    handler = _handler(_store_with_reading(90, T0 - dt.timedelta(seconds=30)), sender)

    run, schedule = _payloads(await handler.handle(_tick(T0)))

    assert [u.target_host for u in sender.sent] == [overlay.hosts[0].name] * 2
    assert {tuple(u.argv)[-2:] for u in sender.sent} == {
        ("--omni-home", overlay.hosts[0].registry_root),
        ("omnimarket.nodes.node_retention_cleanup", "--files"),
    }
    assert [u.outcome for u in run.units] == [EnumHostHygieneUnitOutcome.COMPLETED] * 2
    assert run.outcome is EnumHostHygieneRunOutcome.COMPLETED
    assert all(
        item.reason is EnumHostHygieneReason.DISK_PRESSURE for item in run.decided
    )
    assert schedule.outcome == "completed"
    assert schedule.job_id == overlay.job_id
    assert schedule.tick_id == run.tick_id


async def test_below_every_threshold_is_a_typed_run_with_nothing_sent() -> None:
    sender = InMemoryHostHygieneSender()
    handler = _handler(_store_with_reading(40, T0 - dt.timedelta(seconds=30)), sender)

    run, schedule = _payloads(await handler.handle(_tick(T0)))

    assert sender.sent == []
    assert run.outcome is EnumHostHygieneRunOutcome.NOTHING_DUE
    assert {item.reason for item in run.decided} == {
        EnumHostHygieneReason.BELOW_THRESHOLD
    }
    assert schedule.outcome == "completed"


async def test_a_host_with_no_reading_runs_the_unread_dispatch_cleanup_and_skips_the_other() -> (
    None
):
    sender = InMemoryHostHygieneSender()
    handler = _handler(HostDiskReadingStore(max_hosts=8), sender)

    run, _ = _payloads(await handler.handle(_tick(T0)))

    reasons = {item.cleanup_id: item.reason for item in run.decided}
    assert reasons == {
        "docker_hygiene": EnumHostHygieneReason.UNREAD_RUN_ANYWAY,
        "file_retention": EnumHostHygieneReason.UNREAD_SKIPPED,
    }
    assert len(sender.sent) == 1


async def test_a_stale_reading_reads_as_unread_not_as_pressure() -> None:
    overlay = _overlay()
    sender = InMemoryHostHygieneSender()
    stale = T0 - dt.timedelta(seconds=overlay.max_reading_age_seconds + 1)
    handler = _handler(_store_with_reading(99, stale), sender)

    run, _ = _payloads(await handler.handle(_tick(T0)))

    assert EnumHostHygieneReason.DISK_PRESSURE not in {i.reason for i in run.decided}


async def test_a_tick_inside_the_interval_emits_and_sends_nothing() -> None:
    sender = InMemoryHostHygieneSender()
    handler = _handler(_store_with_reading(90, T0 - dt.timedelta(seconds=30)), sender)
    await handler.handle(_tick(T0))
    sent_after_first = len(sender.sent)

    output = await handler.handle(_tick(T0 + dt.timedelta(seconds=60)))

    assert output.events == ()
    assert len(sender.sent) == sent_after_first


async def test_a_command_that_exits_nonzero_fails_the_run_and_the_schedule_event() -> (
    None
):
    host = _overlay().hosts[0].name
    sender = InMemoryHostHygieneSender(
        {host: (EnumLabWorkUnitStatus.COMPLETED, 3, "boom")}
    )
    handler = _handler(_store_with_reading(90, T0 - dt.timedelta(seconds=30)), sender)

    run, schedule = _payloads(await handler.handle(_tick(T0)))

    assert {u.outcome for u in run.units} == {EnumHostHygieneUnitOutcome.FAILED}
    assert {u.exit_code for u in run.units} == {3}
    assert run.units[0].output_tail == "boom"
    assert run.outcome is EnumHostHygieneRunOutcome.FAILED
    assert schedule.outcome == "failed"


async def test_a_unit_that_exits_nonzero_never_reads_as_completed() -> None:
    # The failing control of test_golden: break the exit-code mapping and this and the test above go red.
    host = _overlay().hosts[0].name
    sender = InMemoryHostHygieneSender({host: (EnumLabWorkUnitStatus.COMPLETED, 1, "")})
    handler = _handler(_store_with_reading(90, T0 - dt.timedelta(seconds=30)), sender)

    run, _ = _payloads(await handler.handle(_tick(T0)))

    assert EnumHostHygieneUnitOutcome.COMPLETED not in {u.outcome for u in run.units}


async def test_a_cleanup_that_ran_waits_its_interval_even_when_it_failed() -> None:
    overlay = _overlay()
    host = overlay.hosts[0].name
    sender = InMemoryHostHygieneSender({host: (EnumLabWorkUnitStatus.COMPLETED, 1, "")})
    handler = _handler(_store_with_reading(90, T0 - dt.timedelta(seconds=30)), sender)
    await handler.handle(_tick(T0))

    # The tick is due again after run_interval_seconds, but docker_hygiene's own interval is longer.
    later = T0 + dt.timedelta(seconds=overlay.run_interval_seconds + 1)
    store = _store_with_reading(90, later - dt.timedelta(seconds=30))
    handler._store = store  # a fresh reading for the later tick
    run, _ = _payloads(await handler.handle(_tick(later)))

    reasons = {item.cleanup_id: item.reason for item in run.decided}
    assert reasons["docker_hygiene"] is EnumHostHygieneReason.INTERVAL_NOT_ELAPSED
    assert len(sender.sent) == 2


async def test_a_unit_that_never_reached_its_host_is_sent_again_on_the_next_due_tick() -> (
    None
):
    overlay = _overlay()
    host = overlay.hosts[0].name
    sender = InMemoryHostHygieneSender(
        {host: (EnumLabWorkUnitStatus.INFRA_ERROR, None, "")}
    )
    handler = _handler(_store_with_reading(90, T0 - dt.timedelta(seconds=30)), sender)

    run, schedule = _payloads(await handler.handle(_tick(T0)))
    assert {u.outcome for u in run.units} == {
        EnumHostHygieneUnitOutcome.TRANSPORT_UNAVAILABLE
    }
    assert schedule.outcome == "failed"

    later = T0 + dt.timedelta(seconds=overlay.run_interval_seconds + 1)
    handler._store = _store_with_reading(90, later - dt.timedelta(seconds=30))
    await handler.handle(_tick(later))

    assert len(sender.sent) == 4


async def test_an_undeclared_overlay_sends_nothing_and_emits_nothing(
    tmp_path: Path,
) -> None:
    sender = InMemoryHostHygieneSender()
    handler = HandlerHostHygieneScheduled(
        gate=ScheduleGate(
            NODE_NAME,
            model=ModelHostHygieneOrchestratorOverlay,
            environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
        ),
        store=HostDiskReadingStore(max_hosts=8),
        sender=sender,
    )

    output = await handler.handle(_tick(T0))

    assert output.events == ()
    assert sender.sent == []
