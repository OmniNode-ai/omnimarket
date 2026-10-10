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
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
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


# --- OMN-20867 AC3: jitter loses no window; one tick is one record ------------------------

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _slot_aligned_ticks(
    start: dt.datetime, periods: list[float], interval_ms: int = TICK_MS
) -> list[ModelRuntimeTick]:
    """The ticks a slot-aligned runtime scheduler emits when its loop wakes ``periods`` apart.

    Each tick carries the epoch-aligned slot it was due on as ``scheduled_at``; the next tick
    is due on the first slot after this tick's ``now``. The wake times run late by whatever
    the period says, as the dev lane's did (1.12 s median against 1000 ms).
    """
    slot = dt.timedelta(milliseconds=interval_ms)
    ticks: list[ModelRuntimeTick] = []
    now = start
    due = _EPOCH + ((start - _EPOCH) // slot) * slot
    for seq, period in enumerate(periods, start=1):
        ticks.append(
            ModelRuntimeTick(
                now=now,
                tick_id=uuid4(),
                sequence_number=seq,
                scheduled_at=due,
                correlation_id=uuid4(),
                scheduler_id="omn20867-test",
                tick_interval_ms=interval_ms,
            )
        )
        due = _EPOCH + ((now - _EPOCH) // slot + 1) * slot
        now = now + dt.timedelta(seconds=period)
    return ticks


def _jittered_periods(count: int) -> list[float]:
    """1.12 s steady, with the slow ticks the dev lane showed (1.56 s p90, 3.3 s max)."""
    pattern = [1.12, 1.12, 1.106, 1.559, 1.12, 1.03, 3.3, 1.12, 1.25, 1.12]
    return [pattern[i % len(pattern)] for i in range(count)]


@pytest.mark.parametrize(
    ("node", "key", "module", "cls", "workflow", "interval", "offset"),
    SCHEDULED,
    ids=IDS,
)
def test_jittered_ticks_fire_every_window_exactly_once(
    node: str,
    key: str,
    module: str,
    cls: str,
    workflow: str,
    interval: int,
    offset: int,
) -> None:
    handler = _handler(node, module, cls)
    # 16:41:09Z for about three hours: crosses 16:50, 17:00 and 17:10, which the dev lane
    # missed, and every window of all three schedules after them.
    start = dt.datetime(2026, 10, 10, 16, 41, 9, 314184, tzinfo=dt.UTC)
    ticks = _slot_aligned_ticks(start, _jittered_periods(9600))
    fired = [
        f.window_start for f in (handler.handle(t) for t in ticks) if f is not None
    ]
    last = ticks[-1].now
    expected = [
        w
        for w in (
            dt.datetime(2026, 10, 10, 16, tzinfo=dt.UTC) + dt.timedelta(seconds=s)
            for s in range(offset, 6 * 3600, interval)
        )
        if start < w <= last
    ]
    assert len(expected) >= 3
    assert fired == expected


def test_jittered_ticks_open_the_audit_trail_daily_slot_exactly_once() -> None:
    from omnimarket.nodes.node_audit_trail_compact_schedule_compute.handlers.handler_audit_trail_compact_schedule import (
        HandlerAuditTrailCompactSchedule,
        schedule_config,
    )

    cfg = schedule_config()
    slot = dt.datetime(
        2026, 10, 10, cfg.run_hour_utc, cfg.run_minute_utc, tzinfo=dt.UTC
    )
    handler = HandlerAuditTrailCompactSchedule()
    for lead in (0.05, 0.3, 0.6, 0.95):
        ticks = _slot_aligned_ticks(
            slot - dt.timedelta(seconds=60 + lead), _jittered_periods(120)
        )
        fires = [t.now for t in ticks if handler.handle(t) is not None]
        assert len(fires) == 1, (lead, fires)
        assert fires[0] >= slot


PRUNE_NODES = ("node_dead_letter_prune_effect", "node_consumer_flow_prune_effect")


async def test_runtime_tick_boot_one_tick_is_one_record_and_dead_letters_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every tick subscriber wired together: the tick and its one fire are the only records.

    The two prune effects returned a result on every tick with no publish topic declared,
    so the boundary dead-lettered each tick twice and the DLQ replay published it back onto
    the tick topic up to five more times; every replay fired the window again.
    """
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("overlay_version: 1.0.0\nenvironment: test\nscope: env\n")
    overlay.chmod(0o600)
    monkeypatch.setenv("OMNIMARKET_PRUNE_BINDING_OVERLAY", str(overlay))
    for name in (
        "ONEX_DEAD_LETTER_ARCHIVE_DIR",
        "ONEX_CONSUMER_FLOW_ARCHIVE_DIR",
        "OMNINODE_INTERNAL_DB_URL",
        "OMNIBASE_INFRA_DB_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    nodes = [row[0] for row in SCHEDULED] + list(PRUNE_NODES)
    manifest = discover_contracts_from_paths(
        [NODES / node / "contract.yaml" for node in nodes]
    )
    assert not manifest.errors, manifest.errors

    bus = EventBusInmemory(environment="omn20867", group="omn20867")
    await bus.start()
    published: list[str] = []
    dead_lettered: list[str] = []
    publish = bus.publish

    async def _recording_publish(topic: str, *args: Any, **kwargs: Any) -> None:
        published.append(topic)
        await publish(topic, *args, **kwargs)

    async def _recording_dlq(**kwargs: Any) -> bool:
        dead_lettered.append(str(kwargs.get("original_topic")))
        return True

    monkeypatch.setattr(bus, "publish", _recording_publish)
    monkeypatch.setattr(bus, "_publish_raw_to_dlq", _recording_dlq, raising=False)

    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    engine.freeze()

    throughput = _contract("node_throughput_tick_decision_compute")["terminal_event"]
    prune_topics = {node: _contract(node).get("terminal_event") for node in PRUNE_NODES}

    # 17:00:00Z opens the throughput window; it is also the first tick the prune
    # effects see, so each runs its scheduled prune once (refused: no binding).
    await bus.publish(
        TICK_TOPIC, None, _tick_wire(_window(0) + dt.timedelta(hours=11)), None
    )
    await asyncio.sleep(2)
    assert dead_lettered == []
    assert published.count(TICK_TOPIC) == 1
    assert published.count(throughput) == 1
    for node, topic in prune_topics.items():
        assert published.count(topic) == 1, node

    # A second tick inside every interval: the tick itself and nothing else.
    published.clear()
    await bus.publish(
        TICK_TOPIC, None, _tick_wire(_window(1) + dt.timedelta(hours=11)), None
    )
    await asyncio.sleep(2)
    await bus.close()
    assert dead_lettered == []
    assert published == [TICK_TOPIC]
