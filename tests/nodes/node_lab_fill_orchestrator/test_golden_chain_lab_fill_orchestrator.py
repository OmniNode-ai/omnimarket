# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lab-fill fire becomes one fire-plan request carrying the held readings (OMN-20867).

The real node_lab_fill_orchestrator contract is wired through ``wire_from_manifest`` onto an
in-memory bus. Capacity advertisements arrive on their contract topic in the serve process's
wire shape, then a lab-fill fire record arrives on the decided topic the removed plan node
used to publish it on (OMN-24579): the orchestrator assembles the headroom request from the
readings it holds and publishes it once per fire_id.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_runtime_tick_schedule import ModelScheduledFire
from omnimarket.nodes.node_lab_fill_orchestrator.handlers import (
    LAB_HOST_READINGS,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
ORCHESTRATOR = "node_lab_fill_orchestrator"
CAPACITY_TOPIC = "onex.evt.omnimarket.lab-host-capacity-advertised.v1"
DECIDED = "onex.evt.omnimarket.lab-fill-plan-decided.v1"
FIRE_PLAN = "onex.cmd.omnimarket.lab-fill-fire-plan-requested.v1"
WINDOW = dt.datetime(2026, 10, 10, 6, 10, tzinfo=dt.UTC)
FIRE_ID = "lab-fill-20261010T061000Z"


@pytest.fixture(autouse=True)
def _empty_readings() -> Iterator[None]:
    LAB_HOST_READINGS.clear()
    yield
    LAB_HOST_READINGS.clear()


def _reading(host: str, *, age_seconds: float) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=host,
        cores=32,
        load1=2.0,
        mem_available_bytes=96 * 1024**3,
        tools=["git", "uv", "claude-login", "codex"],
        running_units=0,
        max_units=1,
        advertised_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=age_seconds),
        cadence_seconds=10,
    )


def _wire(payload: dict[str, object], event_type: str) -> bytes:
    envelope = ModelEventEnvelope[dict[str, object]](
        payload=payload,
        correlation_id=uuid.uuid4(),
        event_type=event_type,
    )
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


def _fire_wire(workflow: str, fire_id: str) -> bytes:
    fire = ModelScheduledFire(
        workflow=workflow,
        fire_id=fire_id,
        window_start=WINDOW,
        interval_seconds=1200,
        tick_id=uuid.uuid4(),
        tick_now=WINDOW,
        scheduler_id="omn24579-test",
    )
    return _wire(fire.model_dump(mode="json"), "omnimarket.lab-fill-plan-decided")


async def _drive(
    readings: list[ModelHostCapacityAdvertisement], fires: list[bytes]
) -> list[dict[str, Any]]:
    manifest = discover_contracts_from_paths([NODES / ORCHESTRATOR / "contract.yaml"])
    assert not manifest.errors, manifest.errors

    bus = EventBusInmemory(environment="omn24579", group="omn24579")
    await bus.start()
    requests: list[dict[str, Any]] = []

    def _collector() -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            body: dict[str, Any] = json.loads(bytes(getattr(message, "value", b"")))
            requests.append(body.get("payload", body))

        return _collect

    await bus.subscribe(FIRE_PLAN, on_message=_collector(), group_id="probe-fire-plan")

    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    engine.freeze()

    for ad in readings:
        await bus.publish(
            CAPACITY_TOPIC,
            ad.host_name.encode(),
            _wire(
                ad.model_dump(mode="json"), "omnimarket.lab-host-capacity-advertised"
            ),
        )
    await asyncio.sleep(0.5)
    for fire in fires:
        await bus.publish(DECIDED, None, fire, None)
        await asyncio.sleep(0.5)
    await asyncio.sleep(2)
    await bus.close()
    return requests


def test_orchestrator_emits_only_the_fire_plan_request() -> None:
    contract: dict[str, Any] = yaml.safe_load(
        (NODES / ORCHESTRATOR / "contract.yaml").read_text()
    )
    assert contract["node_type"] == "orchestrator"
    assert contract["event_bus"]["publish_topics"] == [FIRE_PLAN]
    assert contract["event_bus"]["subscribe_topics"] == [CAPACITY_TOPIC, DECIDED]


async def test_fire_with_fresh_readings_publishes_one_request_naming_the_hosts() -> (
    None
):
    requests = await _drive(
        [
            _reading("h201", age_seconds=5),
            _reading("h202", age_seconds=8),
            _reading("h200", age_seconds=4),
        ],
        [_fire_wire("lab-fill", FIRE_ID)],
    )

    assert len(requests) == 1
    assert requests[0]["fire"]["fire_id"] == FIRE_ID
    assert {r["host_name"] for r in requests[0]["readings"]} == {"h201", "h202", "h200"}


async def test_fire_with_no_reading_at_all_publishes_a_request_with_no_readings() -> (
    None
):
    requests = await _drive([], [_fire_wire("lab-fill", FIRE_ID)])

    assert len(requests) == 1
    assert requests[0]["fire"]["fire_id"] == FIRE_ID
    assert requests[0]["readings"] == []


async def test_replayed_fire_with_the_same_fire_id_publishes_nothing_new() -> None:
    requests = await _drive(
        [_reading("h201", age_seconds=5)],
        [_fire_wire("lab-fill", FIRE_ID)] * 3,
    )

    assert [r["fire"]["fire_id"] for r in requests] == [FIRE_ID]


async def test_fire_of_another_workflow_publishes_nothing() -> None:
    requests = await _drive(
        [_reading("h201", age_seconds=5)],
        [_fire_wire("other", "other-20261010T061000Z")],
    )

    assert requests == []
