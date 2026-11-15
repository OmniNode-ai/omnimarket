# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host-held path of the host hygiene orchestrator (OMN-24557).

A held host (no advertisement, a stale advertisement, or a capacity refusal) is never sent a unit; every one of its
cleanups is decided HOST_HELD with due False, before the interval, pressure and
unread rules.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from pathlib import Path

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_host_hygiene import EnumHostHygieneReason
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneRunOutcome,
    ModelHostHygieneOrchestratorOverlay,
    ModelHostHygieneRunCompleted,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    run_completed_topic,
    schedule_run_topic,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.handler_host_hygiene_scheduled import (
    NODE_NAME,
    HandlerHostHygieneScheduled,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_capacity_readings import (
    HostCapacityStore,
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
        scheduler_id="omn24557-test",
        tick_interval_ms=1000,
    )


def _ad(
    host: str,
    advertised_at: dt.datetime,
    refusal: str = "",
    tools: tuple[str, ...] = (),
) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=host,
        cores=8,
        load1=0.1,
        mem_available_bytes=1 << 30,
        tools=list(tools),
        advertised_at=advertised_at,
        cadence_seconds=10,
        capacity_refusal=refusal,
    )


def _payloads(output) -> ModelHostHygieneRunCompleted:
    run, schedule = output.events
    assert run.event_type == run_completed_topic()
    assert schedule.event_type == schedule_run_topic()
    assert isinstance(run.payload, ModelHostHygieneRunCompleted)
    return run.payload


def _handler(
    capacity: HostCapacityStore,
    sender: InMemoryHostHygieneSender,
    store: HostDiskReadingStore | None = None,
) -> HandlerHostHygieneScheduled:
    overlay = _overlay()
    return HandlerHostHygieneScheduled(
        gate=ScheduleGate(NODE_NAME, declaration=overlay),
        store=store if store is not None else HostDiskReadingStore(max_hosts=8),
        capacity=capacity,
        sender=sender,
        clock=lambda: T0 + dt.timedelta(seconds=5),
    )


@pytest.mark.unit
def test_no_advertisement_is_held() -> None:
    store = HostCapacityStore(max_hosts=8)
    assert (
        store.held_reason_for("h", T0, 3600) == "no capacity advertisement seen for h"
    )


@pytest.mark.unit
def test_stale_advertisement_is_held() -> None:
    store = HostCapacityStore(max_hosts=8)
    store.record(_ad("h", T0 - dt.timedelta(seconds=4000)))
    assert store.held_reason_for("h", T0, 3600) == (
        "capacity advertisement is 4000s old (bound 3600s)"
    )


@pytest.mark.unit
def test_capacity_refusal_is_held() -> None:
    store = HostCapacityStore(max_hosts=8)
    store.record(_ad("h", T0 - dt.timedelta(seconds=10), refusal="full"))
    assert store.held_reason_for("h", T0, 3600) == "capacity refusal: full"


@pytest.mark.unit
def test_a_drain_mark_alone_does_not_hold_a_host_a_cleanup_frees_its_disk() -> None:
    store = HostCapacityStore(max_hosts=8)
    store.record(
        _ad(
            "h",
            T0 - dt.timedelta(seconds=10),
            tools=("draining-until:2026-11-13T13:00:00Z",),
        )
    )
    assert store.held_reason_for("h", T0, 3600) == ""


@pytest.mark.unit
def test_fresh_clean_advertisement_is_not_held() -> None:
    store = HostCapacityStore(max_hosts=8)
    store.record(_ad("h", T0 - dt.timedelta(seconds=10)))
    assert store.held_reason_for("h", T0, 3600) == ""


@pytest.mark.unit
def test_newest_advertisement_wins() -> None:
    store = HostCapacityStore(max_hosts=8)
    store.record(_ad("h", T0 - dt.timedelta(seconds=100), refusal="full"))
    store.record(_ad("h", T0 - dt.timedelta(seconds=10)))
    assert store.held_reason_for("h", T0, 3600) == ""


@pytest.mark.unit
def test_bounded_eviction_drops_the_oldest_host() -> None:
    store = HostCapacityStore(max_hosts=2)
    store.record(_ad("a", T0 - dt.timedelta(seconds=300)))
    store.record(_ad("b", T0 - dt.timedelta(seconds=200)))
    store.record(_ad("c", T0 - dt.timedelta(seconds=100)))
    assert (
        store.held_reason_for("a", T0, 3600) == "no capacity advertisement seen for a"
    )
    assert store.held_reason_for("b", T0, 3600) == ""
    assert store.held_reason_for("c", T0, 3600) == ""


@pytest.mark.unit
def test_a_host_with_no_advertisement_is_sent_nothing_and_reads_host_held() -> None:
    sender = InMemoryHostHygieneSender()
    handler = _handler(HostCapacityStore(max_hosts=8), sender)

    run = _payloads(asyncio.run(handler.handle(_tick(T0))))

    assert sender.sent == []
    assert run.outcome is EnumHostHygieneRunOutcome.NOTHING_DUE
    assert run.units == ()
    assert {item.reason for item in run.decided} == {EnumHostHygieneReason.HOST_HELD}


@pytest.mark.unit
def test_a_fresh_clean_advertisement_still_runs_the_unread_dispatch_cleanup() -> None:
    overlay = _overlay()
    sender = InMemoryHostHygieneSender()
    capacity = HostCapacityStore(max_hosts=8)
    capacity.record(_ad(overlay.hosts[0].name, T0 - dt.timedelta(seconds=10)))
    handler = _handler(capacity, sender)

    run = _payloads(asyncio.run(handler.handle(_tick(T0))))

    reasons = {item.cleanup_id: item.reason for item in run.decided}
    assert reasons == {
        "docker_hygiene": EnumHostHygieneReason.UNREAD_RUN_ANYWAY,
        "file_retention": EnumHostHygieneReason.UNREAD_SKIPPED,
    }
    assert len(sender.sent) == 1


@pytest.mark.unit
def test_a_capacity_refusal_is_sent_nothing_and_reads_host_held() -> None:
    overlay = _overlay()
    sender = InMemoryHostHygieneSender()
    capacity = HostCapacityStore(max_hosts=8)
    capacity.record(
        _ad(overlay.hosts[0].name, T0 - dt.timedelta(seconds=10), refusal="full")
    )
    handler = _handler(capacity, sender)

    run = _payloads(asyncio.run(handler.handle(_tick(T0))))

    assert sender.sent == []
    assert run.outcome is EnumHostHygieneRunOutcome.NOTHING_DUE
    assert run.units == ()
    assert {item.reason for item in run.decided} == {EnumHostHygieneReason.HOST_HELD}


@pytest.mark.unit
def test_the_contract_seam_names_the_capacity_topic_and_model() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    topic = "onex.evt.omnimarket.lab-host-capacity-advertised.v1"
    assert topic in contract["event_bus"]["subscribe_topics"]
    routes = {r["operation"]: r for r in contract["handler_routing"]["handlers"]}
    route = routes["record_host_capacity"]
    assert route["topic"] == topic
    import importlib

    model = getattr(
        importlib.import_module(route["event_model"]["module"]),
        route["event_model"]["name"],
    )
    assert model is ModelHostCapacityAdvertisement
