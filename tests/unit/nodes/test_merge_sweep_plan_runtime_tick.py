# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20867: the merge-sweep plan compute runs from the platform runtime tick.

node_merge_sweep_plan_compute routes platform.runtime-tick to a pure handler that gates on
the interval its contract declares (the cadence of the launchd merge-sweep tick it replaces,
every 300 s): the tick that opens a window publishes that window's fire on the node's terminal
topic, and every other tick inside the interval publishes nothing. The runtime-boot tests load
the real contract from disk, wire it through ``wire_from_manifest`` (the call the kernel makes)
and publish a ModelRuntimeTick envelope on the tick topic; only the bus is in-memory.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib
import json
from collections.abc import Awaitable, Callable
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

pytestmark = pytest.mark.unit

NODE = "node_merge_sweep_plan_compute"
NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
CONTRACT = NODES / NODE / "contract.yaml"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
COMMAND_TOPIC = "onex.cmd.omnimarket.merge-sweep-plan-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.merge-sweep-planned.v1"
HANDLER_MODULE = f"omnimarket.nodes.{NODE}.handlers.handler_merge_sweep_scheduled_fire"
HANDLER_CLASS = "HandlerMergeSweepScheduledFire"
WORKFLOW = "merge-sweep"
INTERVAL = 300
OFFSET = 0
TICK_MS = 1000
ROUTE_OPERATION = "merge_sweep.scheduled_fire"


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(CONTRACT.read_text())
    return loaded


def _handler() -> Any:
    return getattr(importlib.import_module(HANDLER_MODULE), HANDLER_CLASS)()


def _tick(now: dt.datetime, interval_ms: int = TICK_MS) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="omn20867-test",
        tick_interval_ms=interval_ms,
    )


def _window(seconds: int) -> dt.datetime:
    return dt.datetime(2026, 10, 10, 6, 0, tzinfo=dt.UTC) + dt.timedelta(
        seconds=seconds
    )


def test_runtime_tick_contract_routes_to_an_interval_gate() -> None:
    contract = _contract()
    routes = [
        h
        for h in contract["handler_routing"]["handlers"]
        if h.get("event_type") == "platform.runtime-tick"
    ]
    assert len(routes) == 1
    route = routes[0]
    assert route["operation"] == ROUTE_OPERATION
    assert route["topic"] == TICK_TOPIC
    assert route["handler"] == {"name": HANDLER_CLASS, "module": HANDLER_MODULE}
    assert route["event_model"] == (
        "omnibase_infra.runtime.models.model_runtime_tick.ModelRuntimeTick"
    )
    assert route["output_model"] == (
        "omnimarket.models.model_runtime_tick_schedule.ModelScheduledFire"
    )
    assert contract["input_subscriptions"] == [
        {"topic": TICK_TOPIC, "operation": ROUTE_OPERATION}
    ]
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC, TICK_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["descriptor"]["purity"] == "pure"
    schedule = ModelRuntimeTickScheduleConfig.model_validate(
        contract["config"]["merge_sweep_plan"]["schedule"]
    )
    assert (schedule.workflow, schedule.interval_seconds, schedule.offset_seconds) == (
        WORKFLOW,
        INTERVAL,
        OFFSET,
    )


def test_every_command_operation_is_scoped_to_the_command_topic() -> None:
    """No entry spans the command and intent categories (OMN-18013)."""
    entries = _contract()["handler_routing"]["handlers"]
    command = {
        e["operation"]: e.get("topic")
        for e in entries
        if e.get("event_type") != "platform.runtime-tick"
    }
    assert command == dict.fromkeys(
        (
            "plan_merge_sweep_lanes",
            "decide_merge_sweep_lane_retry",
            "render_merge_sweep_lane_brief",
        ),
        COMMAND_TOPIC,
    )


def test_runtime_tick_opening_the_window_fires_and_a_second_tick_inside_does_not() -> (
    None
):
    handler = _handler()
    start = _window(OFFSET)
    fire = handler.handle(_tick(start))
    assert isinstance(fire, ModelScheduledFire)
    assert fire.workflow == WORKFLOW
    assert fire.window_start == start
    assert fire.interval_seconds == INTERVAL
    assert fire.fire_id == f"{WORKFLOW}-{start:%Y%m%dT%H%M%SZ}"
    for later in (1, 2, INTERVAL // 2, INTERVAL - 1):
        assert handler.handle(_tick(start + dt.timedelta(seconds=later))) is None, later
    assert handler.handle(_tick(start - dt.timedelta(seconds=1))) is None
    nxt = handler.handle(_tick(start + dt.timedelta(seconds=INTERVAL)))
    assert isinstance(nxt, ModelScheduledFire)
    assert nxt.fire_id != fire.fire_id


def test_runtime_tick_fires_exactly_once_per_interval_and_holds_no_state() -> None:
    handler = _handler()
    day = dt.datetime(2026, 10, 10, tzinfo=dt.UTC)
    tick_s = 30
    fired = [
        t
        for t in (day + dt.timedelta(seconds=s) for s in range(0, 86400, tick_s))
        if handler.handle(_tick(t, interval_ms=tick_s * 1000)) is not None
    ]
    assert len(fired) == 86400 // INTERVAL
    assert all((t - day).total_seconds() % INTERVAL == OFFSET for t in fired)
    # Stateless: a fresh handler gives the same answer, so a restart repeats no window.
    assert _handler().handle(_tick(fired[0], interval_ms=tick_s * 1000)) is not None


def _tick_wire(now: dt.datetime) -> bytes:
    tick = _tick(now)
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=now,
        event_type="platform.runtime-tick",
    )
    return envelope.model_dump_json().encode("utf-8")


async def _drive(ticks: list[dt.datetime], *, wait_seconds: float) -> list[bytes]:
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors
    assert [c.name for c in manifest.contracts] == [NODE]

    bus = EventBusInmemory(environment="omn20867", group="omn20867")
    await bus.start()
    seen: list[bytes] = []

    def _collector() -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            seen.append(bytes(getattr(message, "value", b"")))

        return _collect

    await bus.subscribe(TERMINAL_TOPIC, on_message=_collector(), group_id="probe")

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

    for now in ticks:
        await bus.publish(TICK_TOPIC, None, _tick_wire(now), None)
    await asyncio.sleep(wait_seconds)
    await bus.close()
    return seen


def _payload(raw: bytes) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(raw)
    payload: dict[str, Any] = body.get("payload", body)
    return payload


async def test_runtime_tick_boot_publishes_one_fire_on_the_terminal_topic() -> None:
    # 06:00:00Z and 06:05:00Z open a window; the ticks one second after each do not.
    seen = await _drive(
        [_window(0), _window(1), _window(300), _window(301)], wait_seconds=3
    )
    fires = sorted(_payload(raw)["fire_id"] for raw in seen)
    assert fires == ["merge-sweep-20261010T060000Z", "merge-sweep-20261010T060500Z"]
    for raw in seen:
        ModelScheduledFire.model_validate(_payload(raw))


async def test_runtime_tick_boot_second_tick_inside_the_interval_publishes_nothing() -> (
    None
):
    seen = await _drive([_window(1), _window(2), _window(301)], wait_seconds=2)
    assert seen == []


_HOST_LOCAL = (
    "import os",
    "from os ",
    "environ",
    "getenv",
    "subprocess",
    "expanduser",
    "Path.home",
    "launchctl",
)


def test_runtime_tick_route_reads_no_host_file_env_or_process() -> None:
    """The tick route runs on a lab runtime: no host path, host env or host process."""
    handler_file = NODES / NODE / "handlers" / "handler_merge_sweep_scheduled_fire.py"
    shared = NODES.parent / "models" / "model_runtime_tick_schedule.py"
    sources = {
        handler_file.name: handler_file.read_text(),
        shared.name: shared.read_text(),
        "contract schedule": yaml.safe_dump(_contract()["config"]["merge_sweep_plan"]),
    }
    found = [
        (name, needle)
        for name, text in sources.items()
        for needle in _HOST_LOCAL
        if needle in text
    ]
    assert found == []
