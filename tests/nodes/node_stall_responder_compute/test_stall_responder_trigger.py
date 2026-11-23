# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stall responder declares what triggers it, and the trigger fires on the runtime tick (OMN-26438).

A merged node nothing triggers is a node that never runs. The contract must declare a trigger: a
schedule on the runtime tick that is routed to a real handler, or another node contract that
publishes the node's command topic. ``declared_triggers`` reads a contract the way that rule
states it, and the control tests show it names no trigger for a contract that declares none.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import importlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
    ModelScheduledFire,
)
from omnimarket.nodes.node_stall_responder_compute.handlers.handler_stall_responder_scheduled_fire import (
    HandlerStallResponderScheduledFire,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
NODE = "node_stall_responder_compute"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"


def _contract(node: str = NODE) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / node / "contract.yaml").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _producers_of(topic: str, own_name: str) -> list[str]:
    """Other node contracts that declare ``topic`` among their publish topics."""
    found: list[str] = []
    for path in sorted(NODES.glob("node_*/contract.yaml")):
        if path.parent.name == own_name:
            continue
        loaded = yaml.safe_load(path.read_text("utf-8"))
        bus = loaded.get("event_bus") if isinstance(loaded, dict) else None
        if isinstance(bus, dict) and topic in (bus.get("publish_topics") or []):
            found.append(path.parent.name)
    return found


def declared_triggers(contract: dict[str, Any], *, producers: list[str]) -> list[str]:
    """The triggers a contract declares; empty when nothing would ever run the node."""
    triggers: list[str] = []
    for key, block in (contract.get("config") or {}).items():
        schedule = block.get("schedule") if isinstance(block, dict) else None
        if schedule is None:
            continue
        ModelRuntimeTickScheduleConfig.model_validate(schedule)
        routes = [
            h
            for h in contract["handler_routing"]["handlers"]
            if h.get("event_type") == "platform.runtime-tick"
        ]
        subscribed = {s["topic"] for s in contract.get("input_subscriptions") or []}
        on_bus = TICK_TOPIC in contract["event_bus"]["subscribe_topics"]
        if routes and TICK_TOPIC in subscribed and on_bus:
            for route in routes:
                module = importlib.import_module(route["handler"]["module"])
                assert callable(getattr(module, route["handler"]["name"])().handle)
            triggers.append(f"schedule:{key}")
    triggers.extend(f"producer:{name}" for name in producers)
    return triggers


def test_the_contract_declares_a_trigger() -> None:
    contract = _contract()
    producers = _producers_of(contract["runtime_dispatch"]["command_topic"], NODE)
    assert declared_triggers(contract, producers=producers), (
        f"{NODE} declares no schedule and no node contract publishes its command topic"
    )


def test_the_schedule_is_the_runtime_tick_shape_and_follows_the_throughput_tick() -> (
    None
):
    contract = _contract()
    schedule = ModelRuntimeTickScheduleConfig.model_validate(
        contract["config"]["stall_responder"]["schedule"]
    )
    throughput = ModelRuntimeTickScheduleConfig.model_validate(
        _contract("node_throughput_tick_decision_compute")["config"][
            "throughput_tick_decision"
        ]["schedule"]
    )
    assert schedule.workflow == "stall-responder"
    assert schedule.interval_seconds == throughput.interval_seconds
    # The tick writes the stall rows; the responder reads them after the tick has written.
    assert schedule.offset_seconds > throughput.offset_seconds
    assert contract["descriptor"]["purity"] == "pure"


def test_the_command_route_keeps_its_own_topic_beside_the_tick_route() -> None:
    contract = _contract()
    by_op = {h["operation"]: h for h in contract["handler_routing"]["handlers"]}
    assert set(by_op) == {"decide_stall_responses", "stall_responder.scheduled_fire"}
    assert (
        by_op["decide_stall_responses"]["topic"]
        == contract["runtime_dispatch"]["command_topic"]
    )
    assert by_op["stall_responder.scheduled_fire"]["topic"] == TICK_TOPIC
    assert contract["input_subscriptions"] == [
        {"topic": TICK_TOPIC, "operation": "stall_responder.scheduled_fire"}
    ]
    assert set(contract["event_bus"]["subscribe_topics"]) == {
        contract["runtime_dispatch"]["command_topic"],
        TICK_TOPIC,
    }


def test_control_a_contract_with_no_schedule_and_no_producer_declares_no_trigger() -> (
    None
):
    bare = copy.deepcopy(_contract())
    del bare["config"]
    assert declared_triggers(bare, producers=[]) == []


def test_control_a_schedule_with_no_tick_route_declares_no_trigger() -> None:
    unrouted = copy.deepcopy(_contract())
    unrouted["handler_routing"]["handlers"] = [
        h
        for h in unrouted["handler_routing"]["handlers"]
        if h.get("event_type") != "platform.runtime-tick"
    ]
    assert declared_triggers(unrouted, producers=[]) == []


def test_control_a_producer_node_alone_declares_a_trigger() -> None:
    bare = copy.deepcopy(_contract())
    del bare["config"]
    assert declared_triggers(bare, producers=["node_x"]) == ["producer:node_x"]


def _tick(now: dt.datetime, interval_ms: int = 1000) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="omn26438-test",
        tick_interval_ms=interval_ms,
    )


def test_the_tick_that_opens_the_window_fires_and_a_second_tick_inside_does_not() -> (
    None
):
    schedule = ModelRuntimeTickScheduleConfig.model_validate(
        _contract()["config"]["stall_responder"]["schedule"]
    )
    handler = HandlerStallResponderScheduledFire()
    start = dt.datetime(2026, 11, 22, 6, 0, tzinfo=dt.UTC) + dt.timedelta(
        seconds=schedule.offset_seconds
    )
    fire = handler.handle(_tick(start))
    assert isinstance(fire, ModelScheduledFire)
    assert fire.workflow == "stall-responder"
    assert fire.fire_id == f"stall-responder-{start:%Y%m%dT%H%M%SZ}"
    assert fire.interval_seconds == schedule.interval_seconds
    for later in (1, 2, schedule.interval_seconds - 1):
        assert handler.handle(_tick(start + dt.timedelta(seconds=later))) is None
    assert handler.handle(_tick(start - dt.timedelta(seconds=1))) is None
    assert isinstance(
        handler.handle(_tick(start + dt.timedelta(seconds=schedule.interval_seconds))),
        ModelScheduledFire,
    )


async def test_the_runtime_boot_wires_the_contract_and_a_tick_publishes_one_fire() -> (
    None
):
    """The real contract through ``wire_from_manifest``; only the bus is in-memory."""
    manifest = discover_contracts_from_paths([NODES / NODE / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    contract = _contract()
    terminal = contract["terminal_event"]
    schedule = ModelRuntimeTickScheduleConfig.model_validate(
        contract["config"]["stall_responder"]["schedule"]
    )
    bus = EventBusInmemory(environment="omn26438", group="omn26438")
    await bus.start()
    seen: list[bytes] = []

    async def _collect(message: object) -> None:
        seen.append(bytes(getattr(message, "value", b"")))

    await bus.subscribe(terminal, on_message=_collect, group_id="probe-omn26438")
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    assert report.total_wired == 1
    engine.freeze()
    start = dt.datetime(2026, 11, 22, 6, 0, tzinfo=dt.UTC) + dt.timedelta(
        seconds=schedule.offset_seconds
    )
    for now in (start, start + dt.timedelta(seconds=1)):
        tick = _tick(now)
        envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
            payload=tick.model_dump(mode="json"),
            correlation_id=tick.correlation_id,
            envelope_timestamp=now,
            event_type="platform.runtime-tick",
        )
        await bus.publish(TICK_TOPIC, None, envelope.model_dump_json().encode(), None)
    await asyncio.sleep(3)
    await bus.close()
    fires = [json.loads(raw) for raw in seen]
    payloads = [body.get("payload", body) for body in fires]
    assert [p["fire_id"] for p in payloads] == [
        f"stall-responder-{start:%Y%m%dT%H%M%SZ}"
    ]
    ModelScheduledFire.model_validate(payloads[0])
