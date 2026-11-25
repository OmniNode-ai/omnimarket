# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The on-demand run route of the host hygiene orchestrator (OMN-24610).

The handler is built the way the runtime builds it: the real schedule gate over the
in-memory profile overlay, the real reading store, an in-memory lab-work sender and a
hold reader; only the sender's answers and the hold reader's held hosts are scripted.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import ValidationError

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.models.model_host_disk_reading import (
    ModelDiskClassReading,
    ModelHostDiskRead,
    ModelHostDiskVolume,
)
from omnimarket.models.model_host_hygiene import EnumHostHygieneReason
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneRunOutcome,
    EnumHostHygieneRunTrigger,
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneOrchestratorOverlay,
    ModelHostHygieneRunCompleted,
)
from omnimarket.models.model_host_hygiene_run_requested import (
    ModelHostHygieneRunRequested,
)
from omnimarket.models.model_lab_work_unit import EnumLabWorkUnitStatus
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    run_completed_topic,
    schedule_run_topic,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.handler_host_hygiene_run_requested import (
    NODE_NAME,
    HandlerHostHygieneRunRequested,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.handler_host_hygiene_scheduled import (
    HandlerHostHygieneScheduled,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.hold_reader import (
    InMemoryHostHoldReader,
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
    store: HostDiskReadingStore,
    sender: InMemoryHostHygieneSender,
    *,
    gate: ScheduleGate | None = None,
    hold_reader: InMemoryHostHoldReader | None = None,
) -> HandlerHostHygieneRunRequested:
    overlay = _overlay()
    return HandlerHostHygieneRunRequested(
        gate=gate or ScheduleGate(NODE_NAME, declaration=overlay),
        store=store,
        sender=sender,
        clock=lambda: T0 + dt.timedelta(seconds=5),
        hold_reader=hold_reader,
    )


def _run_envelope(output: Any) -> tuple[ModelHostHygieneRunCompleted, Any]:
    (envelope,) = output.events
    assert envelope.event_type == run_completed_topic()
    assert isinstance(envelope.payload, ModelHostHygieneRunCompleted)
    return envelope.payload, envelope


def _cid() -> UUID:
    return uuid.uuid4()


async def test_golden_on_demand_run_sends_both_cleanups_and_carries_the_caller_id() -> (
    None
):
    overlay = _overlay()
    cid = _cid()
    sender = InMemoryHostHygieneSender()
    handler = _handler(_store_with_reading(90, T0 - dt.timedelta(seconds=30)), sender)

    output = await handler.handle(ModelHostHygieneRunRequested(correlation_id=cid))

    assert [u.target_host for u in sender.sent] == [overlay.hosts[0].name] * 2
    assert {tuple(u.argv)[-2:] for u in sender.sent} == {
        ("--omni-home", overlay.hosts[0].registry_root),
        ("omnimarket.nodes.node_retention_cleanup", "--files"),
    }
    run, envelope = _run_envelope(output)
    assert envelope.correlation_id == cid
    assert run.outcome is EnumHostHygieneRunOutcome.COMPLETED
    assert run.trigger is EnumHostHygieneRunTrigger.ON_DEMAND
    assert run.tick_id == ""
    assert str(cid) in run.run_id
    assert overlay.job_id in run.run_id
    assert envelope.event_type == run_completed_topic()
    assert schedule_run_topic() not in {e.event_type for e in output.events}


class _SpyGate(ScheduleGate):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.due_calls = 0
        self.mark_calls = 0

    def due(self, now: dt.datetime) -> bool:
        self.due_calls += 1
        return super().due(now)

    def mark(self, now: dt.datetime) -> None:
        self.mark_calls += 1
        super().mark(now)


async def test_ac2_the_on_demand_run_ignores_the_schedule_gate_and_does_not_move_it() -> (
    None
):
    overlay = _overlay()
    cid = _cid()
    sender = InMemoryHostHygieneSender()
    gate = _SpyGate(NODE_NAME, declaration=overlay)
    gate.mark(T0)
    # The schedule says this moment is not due: a scheduled tick would return nothing.
    assert gate.due(T0 + dt.timedelta(seconds=10)) is False
    due_calls_before = gate.due_calls
    mark_calls_before = gate.mark_calls
    last_run_before = gate._last_run
    handler = _handler(
        _store_with_reading(90, T0 - dt.timedelta(seconds=30)), sender, gate=gate
    )

    await handler.handle(ModelHostHygieneRunRequested(correlation_id=cid))

    assert len(sender.sent) == 2
    assert gate.due_calls == due_calls_before
    assert gate.mark_calls == mark_calls_before
    assert gate._last_run == last_run_before == T0


async def test_ac2_a_scheduled_tick_after_an_on_demand_run_is_still_due_and_runs() -> (
    None
):
    overlay = _overlay()
    cid = _cid()
    sender = InMemoryHostHygieneSender()
    gate = ScheduleGate(NODE_NAME, declaration=overlay)
    on_demand = _handler(
        _store_with_reading(90, T0 - dt.timedelta(seconds=30)), sender, gate=gate
    )
    await on_demand.handle(ModelHostHygieneRunRequested(correlation_id=cid))

    scheduled = HandlerHostHygieneScheduled(
        gate=gate,
        store=_store_with_reading(90, T0 - dt.timedelta(seconds=30)),
        sender=sender,
        clock=lambda: T0 + dt.timedelta(seconds=5),
    )
    tick = ModelRuntimeTick(
        now=T0,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=T0,
        correlation_id=uuid.uuid4(),
        scheduler_id="omn24610-test",
        tick_interval_ms=1000,
    )
    output = await scheduled.handle(tick)

    assert len(output.events) == 2
    assert len(sender.sent) == 4


async def test_ac3_a_held_host_sends_nothing_and_reads_held() -> None:
    overlay = _overlay()
    host = overlay.hosts[0].name
    cid = _cid()
    sender = InMemoryHostHygieneSender()
    hold_reader = InMemoryHostHoldReader({host: "heat"})
    handler = _handler(
        HostDiskReadingStore(max_hosts=8), sender, hold_reader=hold_reader
    )

    run, _ = _run_envelope(
        await handler.handle(ModelHostHygieneRunRequested(correlation_id=cid))
    )

    assert sender.sent == []
    assert run.outcome is EnumHostHygieneRunOutcome.NOTHING_DUE
    assert all(item.reason is EnumHostHygieneReason.HELD for item in run.decided)
    assert all(item.held_reason == "heat" for item in run.decided)


async def test_ac3_the_scheduled_handler_with_a_hold_reader_sends_nothing() -> None:
    overlay = _overlay()
    host = overlay.hosts[0].name
    sender = InMemoryHostHygieneSender()
    hold_reader = InMemoryHostHoldReader({host: "heat"})
    handler = HandlerHostHygieneScheduled(
        gate=ScheduleGate(NODE_NAME, declaration=overlay),
        store=HostDiskReadingStore(max_hosts=8),
        sender=sender,
        clock=lambda: T0 + dt.timedelta(seconds=5),
        hold_reader=hold_reader,
    )
    tick = ModelRuntimeTick(
        now=T0,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=T0,
        correlation_id=uuid.uuid4(),
        scheduler_id="omn24610-test",
        tick_interval_ms=1000,
    )
    output = await handler.handle(tick)

    assert sender.sent == []
    (run,) = (e.payload for e in output.events if e.event_type == run_completed_topic())
    assert all(item.reason is EnumHostHygieneReason.HELD for item in run.decided)


async def test_ac3_the_failing_control_with_nothing_held_sends_the_unread_dispatch() -> (
    None
):
    cid = _cid()
    sender = InMemoryHostHygieneSender()
    handler = _handler(
        HostDiskReadingStore(max_hosts=8), sender, hold_reader=InMemoryHostHoldReader()
    )

    run, _ = _run_envelope(
        await handler.handle(ModelHostHygieneRunRequested(correlation_id=cid))
    )

    reasons = {item.cleanup_id: item.reason for item in run.decided}
    assert reasons["docker_hygiene"] is EnumHostHygieneReason.UNREAD_RUN_ANYWAY
    assert len(sender.sent) == 1


def _gate_over(root: Path) -> ScheduleGate:
    return ScheduleGate(
        NODE_NAME,
        model=ModelHostHygieneOrchestratorOverlay,
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
    )


async def test_error_chain_a_missing_overlay_is_refused_under_the_caller_id(
    tmp_path: Path,
) -> None:
    cid = _cid()
    sender = InMemoryHostHygieneSender()
    handler = _handler(
        HostDiskReadingStore(max_hosts=8), sender, gate=_gate_over(tmp_path)
    )

    run, envelope = _run_envelope(
        await handler.handle(ModelHostHygieneRunRequested(correlation_id=cid))
    )

    assert envelope.correlation_id == cid
    assert run.outcome is EnumHostHygieneRunOutcome.REFUSED
    assert run.trigger is EnumHostHygieneRunTrigger.ON_DEMAND
    assert run.job_id == ""
    assert run.detail != ""
    assert run.units == ()
    assert run.decided == ()
    assert sender.sent == []


async def test_error_chain_an_invalid_overlay_is_refused_under_the_caller_id(
    tmp_path: Path,
) -> None:
    data = yaml.safe_load((NODE_DIR / "overlays" / "in_memory.yaml").read_text())
    data["backend"] = "lab_work"
    data["lab_work"]["bus_lane"] = "bus-1"
    data["event_bus"] = {"type": "kafka", "lane": "dev"}
    root = tmp_path / "overlays" / NODE_NAME
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(yaml.safe_dump(data))
    cid = _cid()
    sender = InMemoryHostHygieneSender()
    handler = _handler(
        HostDiskReadingStore(max_hosts=8),
        sender,
        gate=_gate_over(tmp_path / "overlays"),
    )

    run, envelope = _run_envelope(
        await handler.handle(ModelHostHygieneRunRequested(correlation_id=cid))
    )

    assert envelope.correlation_id == cid
    assert run.outcome is EnumHostHygieneRunOutcome.REFUSED
    assert run.detail != ""
    assert sender.sent == []


async def test_error_chain_a_refused_unit_fails_the_run_under_the_caller_id() -> None:
    overlay = _overlay()
    host = overlay.hosts[0].name
    cid = _cid()
    sender = InMemoryHostHygieneSender(
        {host: (EnumLabWorkUnitStatus.REFUSED, None, "held")}
    )
    handler = _handler(_store_with_reading(90, T0 - dt.timedelta(seconds=30)), sender)

    run, envelope = _run_envelope(
        await handler.handle(ModelHostHygieneRunRequested(correlation_id=cid))
    )

    assert envelope.correlation_id == cid
    assert run.outcome is EnumHostHygieneRunOutcome.FAILED
    assert all(u.outcome is EnumHostHygieneUnitOutcome.REFUSED for u in run.units)


async def test_error_chain_a_transport_failure_fails_the_run_under_the_caller_id() -> (
    None
):
    overlay = _overlay()
    host = overlay.hosts[0].name
    cid = _cid()
    sender = InMemoryHostHygieneSender(
        {host: (EnumLabWorkUnitStatus.INFRA_ERROR, None, "no transport")}
    )
    handler = _handler(_store_with_reading(90, T0 - dt.timedelta(seconds=30)), sender)

    run, envelope = _run_envelope(
        await handler.handle(ModelHostHygieneRunRequested(correlation_id=cid))
    )

    assert envelope.correlation_id == cid
    assert run.outcome is EnumHostHygieneRunOutcome.FAILED
    assert all(
        u.outcome is EnumHostHygieneUnitOutcome.TRANSPORT_UNAVAILABLE for u in run.units
    )


def test_the_request_model_requires_a_uuid_correlation_id() -> None:
    with pytest.raises(ValidationError):
        ModelHostHygieneRunRequested.model_validate({})
    cid = uuid.uuid4()
    parsed = ModelHostHygieneRunRequested.model_validate({"correlation_id": str(cid)})
    assert parsed.correlation_id == cid
    with pytest.raises(ValidationError):
        ModelHostHygieneRunRequested.model_validate({"correlation_id": "not-a-uuid"})
    with pytest.raises(ValidationError):
        ModelHostHygieneRunRequested.model_validate(
            {"correlation_id": str(cid), "extra": 1}
        )


async def test_a_hold_reader_that_raises_does_not_stop_the_run(caplog: Any) -> None:
    overlay = _overlay()
    host = overlay.hosts[0].name
    cid = _cid()
    sender = InMemoryHostHygieneSender()

    class _RaisingReader(InMemoryHostHoldReader):
        def held_hosts(self, now: dt.datetime, max_age_seconds: int) -> dict[str, str]:
            raise RuntimeError("secret detail")

    handler = _handler(
        _store_with_reading(90, T0 - dt.timedelta(seconds=30)),
        sender,
        hold_reader=_RaisingReader({host: "heat"}),
    )

    _run_envelope(
        await handler.handle(ModelHostHygieneRunRequested(correlation_id=cid))
    )

    assert len(sender.sent) == 2
    assert "secret detail" not in caplog.text
    assert "RuntimeError" in caplog.text
