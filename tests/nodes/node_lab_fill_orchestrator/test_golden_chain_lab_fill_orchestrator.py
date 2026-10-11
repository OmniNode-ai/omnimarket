# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lab-fill fire ends in one headroom plan or one HEADROOM_UNKNOWN failure (OMN-20867).

The real node_lab_fill_plan_compute and node_lab_fill_orchestrator contracts are wired
through ``wire_from_manifest`` onto an in-memory bus. Capacity advertisements arrive on
their contract topic in the serve process's wire shape, then a runtime tick opens the
lab-fill window: the compute publishes the fire, the orchestrator assembles the headroom
request from the readings it holds and the compute's fire-plan route decides it, so the
result leaves on the decided or the failure terminal.
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
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_lab_fill_orchestrator.handlers import (
    LAB_HOST_READINGS,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
COMPUTE = "node_lab_fill_plan_compute"
ORCHESTRATOR = "node_lab_fill_orchestrator"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
FIRE_PLAN = "onex.cmd.omnimarket.lab-fill-fire-plan-requested.v1"
# 06:10:00Z opens the lab-fill window (interval 1200 s, offset 600 s).
WINDOW = dt.datetime(2026, 10, 10, 6, 10, tzinfo=dt.UTC)
FIRE_ID = "lab-fill-20261010T061000Z"


def _contract(node: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODES / node / "contract.yaml").read_text()
    )
    return loaded


COMPUTE_CONTRACT = _contract(COMPUTE)
DECIDED = COMPUTE_CONTRACT["runtime_dispatch"]["terminal_events"]["success"]
FAILED = COMPUTE_CONTRACT["runtime_dispatch"]["terminal_events"]["failure"]
CAPACITY_TOPIC = COMPUTE_CONTRACT["config"]["lab_fill_plan"]["headroom"][
    "reading_topic"
]


@pytest.fixture(autouse=True)
def _empty_readings() -> Iterator[None]:
    LAB_HOST_READINGS.clear()
    yield
    LAB_HOST_READINGS.clear()


def _reading(
    host: str,
    *,
    age_seconds: float,
    running: int = 0,
    max_units: int = 1,
    load1: float = 2.0,
) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=host,
        cores=32,
        load1=load1,
        mem_available_bytes=96 * 1024**3,
        tools=["git", "uv", "claude-login", "codex"],
        running_units=running,
        max_units=max_units,
        advertised_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=age_seconds),
        cadence_seconds=10,
    )


def _reading_wire(ad: ModelHostCapacityAdvertisement) -> bytes:
    # The serve process's wire shape (omnimarket.lab_work.bus.LabWorkHost.advertise_once).
    envelope = ModelEventEnvelope[dict[str, object]](
        payload=ad.model_dump(mode="json"),
        correlation_id=uuid.uuid4(),
        event_type="omnimarket.lab-host-capacity-advertised",
    )
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


def _tick_wire(now: dt.datetime) -> bytes:
    tick = ModelRuntimeTick(
        now=now,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid.uuid4(),
        scheduler_id="omn20867-test",
        tick_interval_ms=1000,
    )
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=now,
        event_type="platform.runtime-tick",
    )
    return envelope.model_dump_json().encode("utf-8")


async def _drive(
    readings: list[ModelHostCapacityAdvertisement], ticks: list[dt.datetime]
) -> dict[str, list[dict[str, Any]]]:
    manifest = discover_contracts_from_paths(
        [NODES / COMPUTE / "contract.yaml", NODES / ORCHESTRATOR / "contract.yaml"]
    )
    assert not manifest.errors, manifest.errors

    bus = EventBusInmemory(environment="omn20867", group="omn20867")
    await bus.start()
    seen: dict[str, list[dict[str, Any]]] = {DECIDED: [], FAILED: [], FIRE_PLAN: []}

    def _collector(topic: str) -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            body: dict[str, Any] = json.loads(bytes(getattr(message, "value", b"")))
            seen[topic].append(body.get("payload", body))

        return _collect

    for topic in seen:
        await bus.subscribe(
            topic, on_message=_collector(topic), group_id=f"probe-{topic}"
        )

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
        await bus.publish(CAPACITY_TOPIC, ad.host_name.encode(), _reading_wire(ad))
    await asyncio.sleep(0.5)
    for now in ticks:
        await bus.publish(TICK_TOPIC, None, _tick_wire(now), None)
        await asyncio.sleep(0.5)
    await asyncio.sleep(2)
    await bus.close()
    return seen


def test_orchestrator_emits_only_the_compute_fire_plan_command() -> None:
    orchestrator = _contract(ORCHESTRATOR)
    assert orchestrator["node_type"] == "orchestrator"
    assert orchestrator["event_bus"]["publish_topics"] == [FIRE_PLAN]
    assert orchestrator["event_bus"]["subscribe_topics"] == [CAPACITY_TOPIC, DECIDED]
    fire_route = [
        entry
        for entry in COMPUTE_CONTRACT["handler_routing"]["handlers"]
        if entry["operation"] == "plan_lab_fill_fire"
    ]
    assert [entry["topic"] for entry in fire_route] == [FIRE_PLAN]


def _plans(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decided records that are headroom plans, not the schedule's fire records."""
    plans = [row for row in rows if "hosts" in row]
    assert all("fire_id" in row for row in rows if row not in plans), rows
    return plans


async def test_fire_with_fresh_readings_publishes_one_plan_naming_the_idle_hosts() -> (
    None
):
    seen = await _drive(
        [
            _reading("h201", age_seconds=5),
            _reading("h202", age_seconds=8),
            _reading("h200", age_seconds=4, running=1, max_units=1),
        ],
        [WINDOW],
    )

    assert seen[FAILED] == []
    plans = _plans(seen[DECIDED])
    assert len(plans) == 1
    plan = plans[0]
    assert plan["tick_id"] == FIRE_ID
    assert plan["terminal_failure_cause"] is None
    assert plan["unknown"] == []
    lanes = {host["name"]: host["lanes"] for host in plan["hosts"]}
    assert lanes["h201"] > 0
    assert lanes["h202"] > 0
    # h200 runs its one unit: no idle slot, so no lane and no unknown headroom.
    assert lanes["h200"] == 0
    assert plan["free"] == lanes["h201"] + lanes["h202"]


async def test_fire_with_a_stale_reading_fails_headroom_unknown_on_the_failure_topic() -> (
    None
):
    seen = await _drive(
        [_reading("h201", age_seconds=5), _reading("h101", age_seconds=600)],
        [WINDOW],
    )

    assert _plans(seen[DECIDED]) == []
    assert len(seen[FAILED]) == 1
    failure = seen[FAILED][0]
    assert failure["tick_id"] == FIRE_ID
    assert failure["terminal_failure_cause"] == "HEADROOM_UNKNOWN"
    assert [(u["host"], u["reason"]) for u in failure["unknown"]] == [("h101", "stale")]


async def test_fire_with_no_reading_at_all_fails_headroom_unknown() -> None:
    seen = await _drive([], [WINDOW])

    assert _plans(seen[DECIDED]) == []
    assert len(seen[FAILED]) == 1
    failure = seen[FAILED][0]
    assert failure["tick_id"] == FIRE_ID
    assert failure["terminal_failure_cause"] == "HEADROOM_UNKNOWN"
    assert failure["hosts"] == []
    assert failure["free"] == 0


async def test_replayed_fire_with_the_same_fire_id_publishes_nothing_new() -> None:
    # The dev-lane tick topic republishes a tick up to 6 times; each copy re-opens the
    # same window, so the compute publishes the same fire_id again.
    seen = await _drive(
        [_reading("h201", age_seconds=5)],
        [WINDOW, WINDOW, WINDOW + dt.timedelta(milliseconds=300)],
    )

    fires = [row["fire_id"] for row in seen[DECIDED] if "fire_id" in row]
    assert fires == [FIRE_ID] * 3
    assert [row["fire"]["fire_id"] for row in seen[FIRE_PLAN]] == [FIRE_ID]
    assert len(_plans(seen[DECIDED])) == 1
    assert seen[FAILED] == []
