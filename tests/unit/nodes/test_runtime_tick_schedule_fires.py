# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20867: lab-fill, the hourly tick and the merge-throughput tick run from the runtime tick.

Each decision node routes platform.runtime-tick to a pure handler that gates on the interval
its contract declares: the tick that opens a window publishes that window's fire on the node's
terminal topic, and a second tick inside the interval publishes nothing. The runtime-boot
tests load the real contracts from disk, wire them through ``wire_from_manifest`` (the call
the kernel makes) and publish a ModelRuntimeTick envelope on the tick topic; only the bus is
in-memory.
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
from pydantic import ValidationError

from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
    ModelScheduledFire,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
TICK_MS = 1000

# node, contract config key, handler module, handler class, workflow, interval, offset
SCHEDULED = (
    (
        "node_lab_fill_plan_compute",
        "lab_fill_plan",
        "handler_lab_fill_scheduled_fire",
        "HandlerLabFillScheduledFire",
        "lab-fill",
        1200,
        600,
    ),
    (
        "node_hourly_tick_decision_compute",
        "hourly_tick_decision",
        "handler_hourly_tick_scheduled_fire",
        "HandlerHourlyTickScheduledFire",
        "hourly-tick",
        3600,
        900,
    ),
    (
        "node_throughput_tick_decision_compute",
        "throughput_tick_decision",
        "handler_throughput_tick_scheduled_fire",
        "HandlerThroughputTickScheduledFire",
        "throughput-tick",
        600,
        0,
    ),
)
IDS = [row[0] for row in SCHEDULED]


def _contract(node: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODES / node / "contract.yaml").read_text()
    )
    return loaded


def _handler(node: str, module: str, cls: str) -> Any:
    mod = importlib.import_module(f"omnimarket.nodes.{node}.handlers.{module}")
    return getattr(mod, cls)()


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


def _window(offset: int) -> dt.datetime:
    return dt.datetime(2026, 10, 10, 6, 0, tzinfo=dt.UTC) + dt.timedelta(seconds=offset)


@pytest.mark.parametrize(
    ("node", "key", "module", "cls", "workflow", "interval", "offset"),
    SCHEDULED,
    ids=IDS,
)
def test_runtime_tick_contract_routes_to_an_interval_gate(
    node: str,
    key: str,
    module: str,
    cls: str,
    workflow: str,
    interval: int,
    offset: int,
) -> None:
    contract = _contract(node)
    routes = [
        h
        for h in contract["handler_routing"]["handlers"]
        if h.get("event_type") == "platform.runtime-tick"
    ]
    assert len(routes) == 1, f"{node} routes platform.runtime-tick {len(routes)} times"
    route = routes[0]
    assert route["handler"] == {
        "name": cls,
        "module": f"omnimarket.nodes.{node}.handlers.{module}",
    }
    assert route["event_model"] == (
        "omnibase_infra.runtime.models.model_runtime_tick.ModelRuntimeTick"
    )
    assert contract["input_subscriptions"] == [
        {"topic": TICK_TOPIC, "operation": route["operation"]}
    ]
    assert TICK_TOPIC in contract["event_bus"]["subscribe_topics"]
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    assert contract["descriptor"]["purity"] == "pure"
    schedule = ModelRuntimeTickScheduleConfig.model_validate(
        contract["config"][key]["schedule"]
    )
    assert (schedule.workflow, schedule.interval_seconds, schedule.offset_seconds) == (
        workflow,
        interval,
        offset,
    )


@pytest.mark.parametrize(
    ("node", "key", "module", "cls", "workflow", "interval", "offset"),
    SCHEDULED,
    ids=IDS,
)
def test_runtime_tick_opening_the_window_fires_and_a_second_tick_inside_does_not(
    node: str,
    key: str,
    module: str,
    cls: str,
    workflow: str,
    interval: int,
    offset: int,
) -> None:
    handler = _handler(node, module, cls)
    start = _window(offset)
    fire = handler.handle(_tick(start))
    assert isinstance(fire, ModelScheduledFire)
    assert fire.workflow == workflow
    assert fire.window_start == start
    assert fire.interval_seconds == interval
    assert fire.fire_id == f"{workflow}-{start:%Y%m%dT%H%M%SZ}"
    for later in (1, 2, interval // 2, interval - 1):
        assert handler.handle(_tick(start + dt.timedelta(seconds=later))) is None, later
    assert handler.handle(_tick(start - dt.timedelta(seconds=1))) is None
    nxt = handler.handle(_tick(start + dt.timedelta(seconds=interval)))
    assert isinstance(nxt, ModelScheduledFire)
    assert nxt.fire_id != fire.fire_id


@pytest.mark.parametrize(
    ("node", "key", "module", "cls", "workflow", "interval", "offset"),
    SCHEDULED,
    ids=IDS,
)
def test_runtime_tick_fires_exactly_once_per_interval_and_holds_no_state(
    node: str,
    key: str,
    module: str,
    cls: str,
    workflow: str,
    interval: int,
    offset: int,
) -> None:
    handler = _handler(node, module, cls)
    day = dt.datetime(2026, 10, 10, tzinfo=dt.UTC)
    tick_s = 30
    fired = [
        t
        for t in (day + dt.timedelta(seconds=s) for s in range(0, 86400, tick_s))
        if handler.handle(_tick(t, interval_ms=tick_s * 1000)) is not None
    ]
    assert len(fired) == 86400 // interval
    assert all((t - day).total_seconds() % interval == offset % interval for t in fired)
    # Stateless: a fresh handler gives the same answer, so a restart repeats no window.
    again = _handler(node, module, cls)
    assert again.handle(_tick(fired[0], interval_ms=tick_s * 1000)) is not None


def test_runtime_tick_schedule_refuses_an_offset_outside_the_interval() -> None:
    with pytest.raises(ValidationError):
        ModelRuntimeTickScheduleConfig(
            workflow="x", interval_seconds=600, offset_seconds=600
        )
    with pytest.raises(ValidationError):
        ModelRuntimeTickScheduleConfig(workflow="x", interval_seconds=10)


def _tick_wire(now: dt.datetime) -> bytes:
    tick = _tick(now)
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=now,
        event_type="platform.runtime-tick",
    )
    return envelope.model_dump_json().encode("utf-8")


async def _drive(
    ticks: list[dt.datetime], *, wait_seconds: float
) -> dict[str, list[bytes]]:
    paths = [NODES / row[0] / "contract.yaml" for row in SCHEDULED]
    manifest = discover_contracts_from_paths(paths)
    assert not manifest.errors, manifest.errors
    assert {c.name for c in manifest.contracts} == set(IDS)

    bus = EventBusInmemory(environment="omn20867", group="omn20867")
    await bus.start()
    terminals = [_contract(row[0])["terminal_event"] for row in SCHEDULED]
    seen: dict[str, list[bytes]] = {topic: [] for topic in terminals}

    def _collector(topic: str) -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            seen[topic].append(bytes(getattr(message, "value", b"")))

        return _collect

    for topic in terminals:
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
    assert report.total_wired == len(SCHEDULED)
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


async def test_runtime_tick_boot_publishes_one_fire_per_node_on_its_terminal_topic() -> (
    None
):
    # 06:00:00Z opens the throughput window (offset 0) only; 06:10Z opens lab-fill (offset
    # 600) and throughput; 06:15Z opens the hourly tick (offset 900).
    ticks = [
        _window(0),
        _window(1),
        _window(600),
        _window(601),
        _window(900),
        _window(901),
    ]
    seen = await _drive(ticks, wait_seconds=3)
    by_node = {
        row[0]: [_payload(raw) for raw in seen[_contract(row[0])["terminal_event"]]]
        for row in SCHEDULED
    }
    fires = {node: sorted(p["fire_id"] for p in rows) for node, rows in by_node.items()}
    assert fires == {
        "node_lab_fill_plan_compute": ["lab-fill-20261010T061000Z"],
        "node_hourly_tick_decision_compute": ["hourly-tick-20261010T061500Z"],
        "node_throughput_tick_decision_compute": [
            "throughput-tick-20261010T060000Z",
            "throughput-tick-20261010T061000Z",
        ],
    }
    for rows in by_node.values():
        for row in rows:
            ModelScheduledFire.model_validate(row)


async def test_runtime_tick_boot_second_tick_inside_the_interval_publishes_nothing() -> (
    None
):
    seen = await _drive([_window(1), _window(601), _window(901)], wait_seconds=2)
    assert {topic: len(rows) for topic, rows in seen.items()} == dict.fromkeys(seen, 0)


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


@pytest.mark.parametrize(
    ("node", "key", "module", "cls", "workflow", "interval", "offset"),
    SCHEDULED,
    ids=IDS,
)
def test_runtime_tick_route_reads_no_host_file_env_or_process(
    node: str,
    key: str,
    module: str,
    cls: str,
    workflow: str,
    interval: int,
    offset: int,
) -> None:
    """The tick route runs on a lab runtime: no host path, host env or host process."""
    shared = NODES.parent / "models" / "model_runtime_tick_schedule.py"
    sources = {
        f"{node}/handlers/{module}.py": (
            NODES / node / "handlers" / f"{module}.py"
        ).read_text(),
        "models/model_runtime_tick_schedule.py": shared.read_text(),
        f"{node}/contract.yaml schedule": yaml.safe_dump(
            _contract(node)["config"][key]
        ),
    }
    found = [
        (name, needle)
        for name, text in sources.items()
        for needle in _HOST_LOCAL
        if needle in text
    ]
    assert found == []
